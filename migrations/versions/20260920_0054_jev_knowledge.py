"""Durable daily group knowledge jobs and evidence-backed candidates.

Revision ID: 0054_jev_knowledge
Revises: 0053_jev_evaluations
"""
import sqlalchemy as sa
from alembic import op

revision = "0054_jev_knowledge"
down_revision = "0053_jev_evaluations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("jev_knowledge_job",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(256), nullable=False),
        sa.Column("period", sa.Date, nullable=False),
        sa.Column("start_ts", sa.BigInteger, nullable=False),
        sa.Column("end_ts", sa.BigInteger, nullable=False),
        sa.Column("cursor_id", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("scanned", sa.Integer, nullable=False, server_default="0"),
        sa.Column("candidate_count", sa.Integer, nullable=False, server_default="0"),
        *queue_columns(),
        sa.UniqueConstraint("tenant_id", "session_id", "period", name="uq_jev_knowledge_day"),
    )
    op.create_table("jev_knowledge_candidate",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jev_knowledge_job.id"), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(256), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("draft", sa.JSON, nullable=False),
        sa.Column("source_members", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("review", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("comparisons", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("reason", sa.String(96), nullable=False, server_default=""),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("kb_doc_id", sa.BigInteger, nullable=True),
        sa.Column("reviewed_by", sa.String(256), nullable=False, server_default=""),
        *queue_columns(),
        sa.UniqueConstraint("tenant_id", "session_id", "fingerprint", name="uq_jev_knowledge_candidate"),
    )
    for table in ("jev_knowledge_job", "jev_knowledge_candidate"):
        op.create_index("ix_" + table + "_due", table, ["status", "next_run_at"])
        op.create_index("ix_" + table + "_scope", table, ["tenant_id", "session_id", "created_at"])


def queue_columns():
    return [
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_type", sa.String(96), nullable=False, server_default=""),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def downgrade():
    op.drop_table("jev_knowledge_candidate")
    op.drop_table("jev_knowledge_job")
