"""add kb document content

Revision ID: 0004_kb_document_content
Revises: 0003_plugin_state
Create Date: 2026-04-24

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_kb_document_content"
down_revision = "0003_plugin_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kb_documents",
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("kb_documents", "content")
