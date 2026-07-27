"""Version wxbot/repeater policies and add atomic aggregate state.

Revision ID: 0024_reply_policy_atomicity
Revises: 0023_plugin_config_versions
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024_reply_policy_atomicity"
down_revision = "0023_plugin_config_versions"
branch_labels = None
depends_on = None

_VERSIONED_CONFIG_TABLES = (
    ("plugin_wxbot_tenant_policy", "ck_wxbot_tenant_policy_version_positive"),
    ("plugin_wxbot_session_policy", "ck_wxbot_session_policy_version_positive"),
    ("plugin_repeater_config", "ck_repeater_config_version_positive"),
)


def upgrade() -> None:
    for table_name, constraint_name in _VERSIONED_CONFIG_TABLES:
        op.add_column(
            table_name,
            sa.Column(
                "version",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )
        op.create_check_constraint(
            constraint_name,
            table_name,
            "version > 0",
        )

    op.create_table(
        "plugin_wxbot_reply_policy_aggregate_state",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=256), nullable=False),
        sa.Column(
            "sdk_group_require_at_me",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "effect_idempotency_key",
            sa.String(length=512),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "version >= 0",
            name="ck_wxbot_reply_aggregate_version_nonnegative",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "session_id",
            name="pk_wxbot_reply_policy_aggregate_state",
        ),
    )
    op.create_index(
        "ix_wxbot_reply_aggregate_effect",
        "plugin_wxbot_reply_policy_aggregate_state",
        ["tenant_id", "effect_idempotency_key"],
    )

    op.create_table(
        "plugin_wxbot_reply_policy_idempotency",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("session_id", sa.String(length=256), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "response_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "response_etag",
            sa.String(length=160),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "completed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
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
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "idempotency_key",
            name="pk_wxbot_reply_policy_idempotency",
        ),
    )
    op.create_index(
        "ix_wxbot_reply_policy_idempotency_created",
        "plugin_wxbot_reply_policy_idempotency",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wxbot_reply_policy_idempotency_created",
        table_name="plugin_wxbot_reply_policy_idempotency",
    )
    op.drop_table("plugin_wxbot_reply_policy_idempotency")
    op.drop_index(
        "ix_wxbot_reply_aggregate_effect",
        table_name="plugin_wxbot_reply_policy_aggregate_state",
    )
    op.drop_table("plugin_wxbot_reply_policy_aggregate_state")

    for table_name, constraint_name in reversed(_VERSIONED_CONFIG_TABLES):
        op.drop_constraint(constraint_name, table_name, type_="check")
        op.drop_column(table_name, "version")
