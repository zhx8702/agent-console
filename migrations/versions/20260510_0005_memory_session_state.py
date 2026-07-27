"""add memory session rolling state

Revision ID: 0005_memory_session_state
Revises: 0004_kb_document_content
Create Date: 2026-05-10

"""
from __future__ import annotations

from alembic import op

revision = "0005_memory_session_state"
down_revision = "0004_kb_document_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE IF EXISTS plugin_memory_session_profile ADD COLUMN IF NOT EXISTS session_summary TEXT DEFAULT ''")
    op.execute("ALTER TABLE IF EXISTS plugin_memory_session_profile ADD COLUMN IF NOT EXISTS open_items_json TEXT DEFAULT '[]'")
    op.execute("ALTER TABLE IF EXISTS plugin_memory_session_profile ADD COLUMN IF NOT EXISTS decisions_json TEXT DEFAULT '[]'")
    op.execute("ALTER TABLE IF EXISTS plugin_memory_session_profile ADD COLUMN IF NOT EXISTS recent_turns_json TEXT DEFAULT '[]'")
    op.execute("ALTER TABLE IF EXISTS plugin_memory_session_profile ADD COLUMN IF NOT EXISTS last_compacted_at TIMESTAMP NULL")
    op.execute("ALTER TABLE IF EXISTS plugin_memory_session_profile ADD COLUMN IF NOT EXISTS summary_version INTEGER DEFAULT 1")


def downgrade() -> None:
    op.execute("ALTER TABLE IF EXISTS plugin_memory_session_profile DROP COLUMN IF EXISTS summary_version")
    op.execute("ALTER TABLE IF EXISTS plugin_memory_session_profile DROP COLUMN IF EXISTS last_compacted_at")
    op.execute("ALTER TABLE IF EXISTS plugin_memory_session_profile DROP COLUMN IF EXISTS recent_turns_json")
    op.execute("ALTER TABLE IF EXISTS plugin_memory_session_profile DROP COLUMN IF EXISTS decisions_json")
    op.execute("ALTER TABLE IF EXISTS plugin_memory_session_profile DROP COLUMN IF EXISTS open_items_json")
    op.execute("ALTER TABLE IF EXISTS plugin_memory_session_profile DROP COLUMN IF EXISTS session_summary")
