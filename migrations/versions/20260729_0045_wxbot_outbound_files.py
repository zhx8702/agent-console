"""Add durable outbound file metadata to the wxbot reply queue.

Revision ID: 0045_wxbot_outbound_files
Revises: 0044_persona_profile_catalog
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045_wxbot_outbound_files"
down_revision = "0044_persona_profile_catalog"
branch_labels = None
depends_on = None

_TABLE = "plugin_wxbot_reply_queue"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("file_path", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        _TABLE,
        sa.Column("file_name", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        _TABLE,
        sa.Column("file_size", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("file_md5", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        _TABLE,
        sa.Column("file_sha256", sa.Text(), nullable=False, server_default=""),
    )
    op.execute(
        "UPDATE app_schema_contract SET compatibility_level = 8 "
        "WHERE contract_name = 'agent-console-runtime'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE app_schema_contract SET compatibility_level = 7 "
        "WHERE contract_name = 'agent-console-runtime'"
    )
    op.drop_column(_TABLE, "file_sha256")
    op.drop_column(_TABLE, "file_md5")
    op.drop_column(_TABLE, "file_size")
    op.drop_column(_TABLE, "file_name")
    op.drop_column(_TABLE, "file_path")
