"""add session scope to faq and knowledge base

Revision ID: 0002_kb_scope_session
Revises: 0001_initial
Create Date: 2026-04-21

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_kb_scope_session"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "faqs",
        sa.Column("session_id", sa.String(length=128), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_faqs_tenant_session_status",
        "faqs",
        ["tenant_id", "session_id", "status"],
    )

    op.add_column(
        "kb_documents",
        sa.Column("session_id", sa.String(length=128), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_kb_documents_tenant_session",
        "kb_documents",
        ["tenant_id", "session_id"],
    )
    op.create_index(
        "ix_kb_documents_tenant_session_hash",
        "kb_documents",
        ["tenant_id", "session_id", "content_hash"],
    )

    op.add_column(
        "kb_chunks",
        sa.Column("session_id", sa.String(length=128), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_kb_chunks_tenant_session_doc",
        "kb_chunks",
        ["tenant_id", "session_id", "doc_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_kb_chunks_tenant_session_doc", table_name="kb_chunks")
    op.drop_column("kb_chunks", "session_id")

    op.drop_index("ix_kb_documents_tenant_session_hash", table_name="kb_documents")
    op.drop_index("ix_kb_documents_tenant_session", table_name="kb_documents")
    op.drop_column("kb_documents", "session_id")

    op.drop_index("ix_faqs_tenant_session_status", table_name="faqs")
    op.drop_column("faqs", "session_id")
