"""Scope memory dedupe by the immutable audience contract.

Revision ID: 0031_memory_audience_dedupe
Revises: 0030_plugin_scope_concurrency
Create Date: 2026-07-18
"""

from __future__ import annotations

from alembic import op

revision = "0031_memory_audience_dedupe"
down_revision = "0030_plugin_scope_concurrency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_memory_item_dedupe")
    op.execute(
        "CREATE UNIQUE INDEX ux_memory_item_dedupe ON plugin_memory_item "
        "(tenant_id, channel, source_key, user_id, scope_type, session_id, "
        "source_type, normalized_key, origin_session_kind, audience_scope, "
        "md5(allowed_session_ids::text)) WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_memory_item_dedupe")
    op.execute(
        "CREATE UNIQUE INDEX ux_memory_item_dedupe ON plugin_memory_item "
        "(tenant_id, channel, source_key, user_id, scope_type, session_id, "
        "source_type, normalized_key) WHERE deleted_at IS NULL"
    )
