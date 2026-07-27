"""Index the globally serialized plugin lifecycle queue.

Revision ID: 0035_plugin_lifecycle_global_index
Revises: 0034_message_effect_producer_owner
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_plugin_lifecycle_global_index"
down_revision = "0034_message_effect_producer_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_plugin_lifecycle_in_progress_created",
        "plugin_lifecycle_operation",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'in_progress'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_plugin_lifecycle_in_progress_created",
        table_name="plugin_lifecycle_operation",
    )
