"""add durable social participation, privacy, voice, and audit contracts

Revision ID: 0018_social_policy_contract
Revises: 0017_message_reliability
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018_social_policy_contract"
down_revision = "0017_message_reliability"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "social_group_policy",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=256), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "global_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "tenant_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "group_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "policy_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "voice_profile_id",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "updated_by",
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
        sa.CheckConstraint("version > 0", name="ck_social_group_policy_version"),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "session_id",
            name="pk_social_group_policy",
        ),
    )

    op.create_table(
        "social_group_policy_history",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=256), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rollback_from_version", sa.Integer(), nullable=True),
        sa.Column("snapshot_json", JSONB, nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False, server_default=""),
        sa.Column(
            "change_reason",
            sa.String(length=240),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_social_group_policy_history"),
        sa.UniqueConstraint(
            "tenant_id",
            "session_id",
            "version",
            name="uq_social_group_policy_history_version",
        ),
    )
    op.create_index(
        "ix_social_group_policy_history_scope_created",
        "social_group_policy_history",
        ["tenant_id", "session_id", "created_at"],
    )

    op.create_table(
        "social_member_policy",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=256), nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "policy_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_by",
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
        sa.CheckConstraint("version > 0", name="ck_social_member_policy_version"),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "session_id",
            "user_id",
            name="pk_social_member_policy",
        ),
    )

    op.create_table(
        "social_member_policy_history",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=256), nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rollback_from_version", sa.Integer(), nullable=True),
        sa.Column("snapshot_json", JSONB, nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False, server_default=""),
        sa.Column(
            "change_reason",
            sa.String(length=240),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_social_member_policy_history"),
        sa.UniqueConstraint(
            "tenant_id",
            "session_id",
            "user_id",
            "version",
            name="uq_social_member_policy_history_version",
        ),
    )
    op.create_index(
        "ix_social_member_policy_history_scope_created",
        "social_member_policy_history",
        ["tenant_id", "session_id", "user_id", "created_at"],
    )

    op.create_table(
        "social_participation_event",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=256), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("event_kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column(
            "reason_codes_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "signal_summary_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("trace_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "event_kind IN ('preview', 'runtime')",
            name="ck_social_participation_event_kind",
        ),
        sa.CheckConstraint(
            "status IN ('must_reply', 'may_reply', 'observe_only', 'defer', 'cancel')",
            name="ck_social_participation_event_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_social_participation_event"),
    )
    op.create_index(
        "ix_social_participation_event_scope_created",
        "social_participation_event",
        ["tenant_id", "session_id", "created_at"],
    )

    op.create_table(
        "voice_profile",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=256), nullable=False),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "profile_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("updated_by", sa.String(length=128), nullable=False, server_default=""),
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
        sa.CheckConstraint("version >= 0", name="ck_voice_profile_version"),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "session_id",
            "profile_id",
            name="pk_voice_profile",
        ),
    )
    op.create_index(
        "ix_voice_profile_scope_updated",
        "voice_profile",
        ["tenant_id", "session_id", "updated_at"],
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("user_id", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("actor_kind", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
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
        sa.Column("policy_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trace_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column(
            "idempotency_key",
            sa.String(length=128),
            nullable=False,
            server_default="",
        ),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )
    op.create_index(
        "ix_audit_events_scope_created",
        "audit_events",
        ["tenant_id", "session_id", "created_at"],
    )
    op.create_index("ix_audit_events_trace", "audit_events", ["trace_id"])

    op.create_table(
        "social_policy_idempotency",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("resource_kind", sa.String(length=32), nullable=False),
        sa.Column("resource_key", sa.String(length=640), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_json", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_social_policy_idempotency"),
        sa.UniqueConstraint(
            "tenant_id",
            "resource_kind",
            "resource_key",
            "idempotency_key",
            name="uq_social_policy_idempotency_scope_key",
        ),
    )
    op.create_index(
        "ix_social_policy_idempotency_created",
        "social_policy_idempotency",
        ["created_at"],
    )

    # Existing rows are private by default.  Nothing becomes visible to a
    # group merely because this migration is applied.
    op.add_column(
        "plugin_memory_item",
        sa.Column(
            "audience_scope",
            sa.String(length=32),
            nullable=False,
            server_default="private",
        ),
    )
    op.add_column(
        "plugin_memory_item",
        sa.Column(
            "origin_session_kind",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "plugin_memory_item",
        sa.Column(
            "allowed_session_ids",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "plugin_memory_item",
        sa.Column(
            "source_kind",
            sa.String(length=32),
            nullable=False,
            server_default="conversation",
        ),
    )
    op.add_column(
        "plugin_memory_item",
        sa.Column(
            "sensitivity_category",
            sa.String(length=32),
            nullable=False,
            server_default="normal",
        ),
    )
    op.add_column(
        "plugin_memory_item",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_memory_item_audience_expiry",
        "plugin_memory_item",
        ["tenant_id", "user_id", "audience_scope", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_item_audience_expiry", table_name="plugin_memory_item")
    for column_name in (
        "expires_at",
        "sensitivity_category",
        "source_kind",
        "allowed_session_ids",
        "origin_session_kind",
        "audience_scope",
    ):
        op.drop_column("plugin_memory_item", column_name)

    op.drop_index(
        "ix_social_policy_idempotency_created",
        table_name="social_policy_idempotency",
    )
    op.drop_table("social_policy_idempotency")
    op.drop_index("ix_audit_events_trace", table_name="audit_events")
    op.drop_index("ix_audit_events_scope_created", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_voice_profile_scope_updated", table_name="voice_profile")
    op.drop_table("voice_profile")
    op.drop_index(
        "ix_social_participation_event_scope_created",
        table_name="social_participation_event",
    )
    op.drop_table("social_participation_event")
    op.drop_index(
        "ix_social_member_policy_history_scope_created",
        table_name="social_member_policy_history",
    )
    op.drop_table("social_member_policy_history")
    op.drop_table("social_member_policy")
    op.drop_index(
        "ix_social_group_policy_history_scope_created",
        table_name="social_group_policy_history",
    )
    op.drop_table("social_group_policy_history")
    op.drop_table("social_group_policy")
