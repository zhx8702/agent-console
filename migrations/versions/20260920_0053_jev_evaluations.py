"""Durable Jev evaluation queue and tenant policy.

Revision ID: 0053_jev_evaluations
Revises: 0052_wxbot_group_webhooks
"""
import sqlalchemy as sa
from alembic import op

revision = "0053_jev_evaluations"
down_revision = "0052_wxbot_group_webhooks"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("jev_policy",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("policy", sa.JSON, nullable=False),
    )
    op.create_table("jev_evaluation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(256), nullable=False, server_default=""),
        sa.Column("domain", sa.String(32), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("target_id", sa.BigInteger, nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.JSON, nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("result", sa.JSON, nullable=True),
        sa.Column("error_type", sa.String(96), nullable=False, server_default=""),
        sa.Column("duration_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("applied", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "domain", "fingerprint", name="uq_jev_evaluation_input"),
    )
    op.create_index("ix_jev_due", "jev_evaluation", ["status", "next_run_at"])
    op.create_index("ix_jev_tenant_created", "jev_evaluation", ["tenant_id", "created_at"])


def downgrade():
    op.drop_table("jev_evaluation")
    op.drop_table("jev_policy")
