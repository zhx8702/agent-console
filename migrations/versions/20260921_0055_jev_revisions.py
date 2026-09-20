"""Auditable knowledge revision proposals and daily Jev quality findings.

Revision ID: 0055_jev_revisions
Revises: 0054_jev_knowledge
"""
import sqlalchemy as sa
from alembic import op

revision = "0055_jev_revisions"
down_revision = "0054_jev_knowledge"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jev_knowledge_candidate", sa.Column("revision", sa.JSON, nullable=False, server_default="{}"))
    op.add_column("jev_knowledge_job", sa.Column("quality_count", sa.Integer, nullable=False, server_default="0"))
    op.create_table("jev_quality_finding",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jev_knowledge_job.id"), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(256), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("finding", sa.JSON, nullable=False),
        sa.Column("source_members", sa.JSON, nullable=False),
        sa.Column("review", sa.JSON, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(96), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "session_id", "fingerprint", name="uq_jev_quality_finding"),
    )
    op.create_index("ix_jev_quality_scope", "jev_quality_finding", ["tenant_id", "session_id", "created_at"])


def downgrade():
    op.drop_table("jev_quality_finding")
    op.drop_column("jev_knowledge_job", "quality_count")
    op.drop_column("jev_knowledge_candidate", "revision")
