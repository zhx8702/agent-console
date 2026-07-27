"""add independent social scope and tenant-member controls

Revision ID: 0022_social_scope_controls
Revises: 0021_turn_tenant_writer_compat
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022_social_scope_controls"
down_revision = "0021_turn_tenant_writer_compat"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column(
        "social_participation_event",
        sa.Column("runtime_stage", sa.String(length=32), nullable=False, server_default="decision"),
    )
    op.add_column(
        "social_participation_event",
        sa.Column(
            "delivery_stage", sa.String(length=32), nullable=False, server_default="not_applicable"
        ),
    )
    op.create_table(
        "social_scope_control",
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rollout_stage", sa.String(length=32), nullable=False, server_default="shadow"),
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
        sa.CheckConstraint(
            "scope_kind IN ('global', 'tenant')", name="ck_social_scope_control_kind"
        ),
        sa.CheckConstraint("version > 0", name="ck_social_scope_control_version"),
        sa.PrimaryKeyConstraint("scope_kind", "tenant_id", name="pk_social_scope_control"),
    )
    op.create_table(
        "social_scope_control_history",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snapshot_json", JSONB, nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("change_reason", sa.String(length=240), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_social_scope_control_history"),
        sa.UniqueConstraint(
            "scope_kind", "tenant_id", "version", name="uq_social_scope_control_history_version"
        ),
    )
    op.create_index(
        "ix_social_scope_control_history_scope_created",
        "social_scope_control_history",
        ["scope_kind", "tenant_id", "created_at"],
    )
    op.create_table(
        "social_tenant_member_control",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("memory_opt_out", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("participation_opt_out", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("no_group_mentions", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deletion_state", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("deletion_intent_key", sa.String(length=512), nullable=False, server_default=""),
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
        sa.CheckConstraint("version > 0", name="ck_social_tenant_member_control_version"),
        sa.CheckConstraint(
            "deletion_state IN ('none', 'requested', 'completed', 'failed')",
            name="ck_social_tenant_member_control_deletion_state",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", name="pk_social_tenant_member_control"),
    )
    op.create_table(
        "voice_profile_history",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=256), nullable=False),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rollback_from_version", sa.Integer(), nullable=True),
        sa.Column("snapshot_json", JSONB, nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("change_reason", sa.String(length=240), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_voice_profile_history"),
        sa.UniqueConstraint(
            "tenant_id",
            "session_id",
            "version",
            name="uq_voice_profile_history_scope_version",
        ),
    )
    op.create_index(
        "ix_voice_profile_history_scope_created",
        "voice_profile_history",
        ["tenant_id", "session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_voice_profile_history_scope_created",
        table_name="voice_profile_history",
    )
    op.drop_table("voice_profile_history")
    op.drop_table("social_tenant_member_control")
    op.drop_index(
        "ix_social_scope_control_history_scope_created",
        table_name="social_scope_control_history",
    )
    op.drop_table("social_scope_control_history")
    op.drop_table("social_scope_control")
    op.drop_column("social_participation_event", "delivery_stage")
    op.drop_column("social_participation_event", "runtime_stage")
