"""add draw task persistence table

Revision ID: 0007_plugin_draw_task
Revises: 0006_memory_graph
Create Date: 2026-05-11

"""
from __future__ import annotations

from alembic import op

revision = "0007_plugin_draw_task"
down_revision = "0006_memory_graph"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS plugin_draw_task (
            task_id                     VARCHAR(64) PRIMARY KEY,
            request_id                  VARCHAR(128) DEFAULT '',
            trace_id                    VARCHAR(128) DEFAULT '',
            command_type                VARCHAR(32) NOT NULL,
            status                      VARCHAR(16) NOT NULL DEFAULT 'queued',
            tenant_id                   VARCHAR(64) DEFAULT '',
            channel                     VARCHAR(32) DEFAULT '',
            source_key                  TEXT DEFAULT '',
            chat_id                     VARCHAR(256) DEFAULT '',
            session_id                  VARCHAR(256) DEFAULT '',
            group_id                    VARCHAR(256) DEFAULT '',
            user_id                     VARCHAR(256) DEFAULT '',
            requester                   VARCHAR(256) DEFAULT '',
            requester_display_name      VARCHAR(256) DEFAULT '',
            original_message_id         VARCHAR(128) DEFAULT '',
            callback_target_json        TEXT DEFAULT '{}',
            callback_reply_to_message_id VARCHAR(128) DEFAULT '',
            source_message_json         TEXT DEFAULT '{}',
            prompt                      TEXT DEFAULT '',
            quality                     VARCHAR(16) DEFAULT 'low',
            size                        VARCHAR(32) DEFAULT '',
            source_image_json           TEXT DEFAULT '{}',
            result_image_id             VARCHAR(128) DEFAULT '',
            result_local_path           TEXT DEFAULT '',
            result_file_name            TEXT DEFAULT '',
            result_media_type           VARCHAR(128) DEFAULT '',
            result_public_path          TEXT DEFAULT '',
            result_source_url           TEXT DEFAULT '',
            error_code                  VARCHAR(128) DEFAULT '',
            error_message               TEXT DEFAULT '',
            callback_sent               BOOLEAN NOT NULL DEFAULT FALSE,
            callback_error              TEXT DEFAULT '',
            created_at                  TIMESTAMPTZ NOT NULL,
            updated_at                  TIMESTAMPTZ NOT NULL,
            started_at                  TIMESTAMPTZ,
            finished_at                 TIMESTAMPTZ,
            heartbeat_at                TIMESTAMPTZ
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_draw_task_status_heartbeat
        ON plugin_draw_task (status, heartbeat_at, updated_at)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_draw_task_tenant_created
        ON plugin_draw_task (tenant_id, created_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_draw_task_tenant_created")
    op.execute("DROP INDEX IF EXISTS idx_draw_task_status_heartbeat")
    op.execute("DROP TABLE IF EXISTS plugin_draw_task")
