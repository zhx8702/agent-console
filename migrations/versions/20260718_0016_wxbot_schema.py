"""create and adopt the complete wxbot schema

Revision ID: 0016_wxbot_schema
Revises: 0015_runtime_plugin_schema
Create Date: 2026-07-18

Earlier wxbot migrations could only alter a table created by application
startup.  This migration creates every wxbot table first, then applies
idempotent compatibility upgrades for existing installations.
"""

from __future__ import annotations

from alembic import op

revision = "0016_wxbot_schema"
down_revision = "0015_runtime_plugin_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _reply_queue_schema()
    _event_schema()
    _group_context_schema()
    _policy_schema()
    _report_schema()


def downgrade() -> None:
    # Preserve tables adopted from pre-Alembic runtime bootstraps.
    pass


def _execute(*statements: str) -> None:
    for statement in statements:
        op.execute(statement)


def _reply_queue_schema() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_wxbot_reply_queue (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            session_name VARCHAR(256) DEFAULT '',
            sender_name VARCHAR(256) DEFAULT '',
            sender_wxid VARCHAR(256) DEFAULT '',
            mention_sender BOOLEAN NOT NULL DEFAULT FALSE,
            reply_to_msg_svr_id VARCHAR(128) DEFAULT '',
            session_kind VARCHAR(32) DEFAULT '',
            reply_text TEXT NOT NULL,
            msg_type VARCHAR(16) DEFAULT 'text',
            image_path TEXT DEFAULT '',
            image_url TEXT DEFAULT '',
            source_message_json TEXT DEFAULT '',
            delivery_json TEXT DEFAULT '',
            command_id VARCHAR(256) DEFAULT '',
            sdk_outbound_id BIGINT,
            channel VARCHAR(32) DEFAULT 'wechat',
            trace_id VARCHAR(128) DEFAULT '',
            participation_status VARCHAR(24) DEFAULT '',
            source_message_id VARCHAR(128) DEFAULT '',
            not_before TIMESTAMPTZ,
            expires_at TIMESTAMPTZ,
            status VARCHAR(16) DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            claim_owner VARCHAR(128) NOT NULL DEFAULT '',
            claim_token VARCHAR(64) NOT NULL DEFAULT '',
            claim_until TIMESTAMPTZ,
            error TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            queued_at TIMESTAMP,
            sent_at TIMESTAMP
        )
        """,
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "mention_sender BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "msg_type VARCHAR(16) DEFAULT 'text'",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "image_path TEXT DEFAULT ''",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "image_url TEXT DEFAULT ''",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "sender_wxid VARCHAR(256) DEFAULT ''",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "reply_to_msg_svr_id VARCHAR(128) DEFAULT ''",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "session_kind VARCHAR(32) DEFAULT ''",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "source_message_json TEXT DEFAULT ''",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "delivery_json TEXT DEFAULT ''",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "command_id VARCHAR(256) DEFAULT ''",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS sdk_outbound_id BIGINT",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS queued_at TIMESTAMP",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "participation_status VARCHAR(24) DEFAULT ''",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "source_message_id VARCHAR(128) DEFAULT ''",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS not_before TIMESTAMPTZ",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
        "UPDATE plugin_wxbot_reply_queue SET attempt_count = 0 WHERE attempt_count IS NULL",
        "ALTER TABLE plugin_wxbot_reply_queue ALTER COLUMN attempt_count SET DEFAULT 0",
        "ALTER TABLE plugin_wxbot_reply_queue ALTER COLUMN attempt_count SET NOT NULL",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "claim_owner VARCHAR(128) NOT NULL DEFAULT ''",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "claim_token VARCHAR(64) NOT NULL DEFAULT ''",
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS claim_until TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS idx_wxbot_reply_queue_status "
        "ON plugin_wxbot_reply_queue (tenant_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_wxbot_reply_queue_command_id "
        "ON plugin_wxbot_reply_queue (command_id)",
        "DROP INDEX IF EXISTS idx_wxbot_reply_queue_command_id_unique",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_wxbot_reply_queue_tenant_command_id_unique "
        "ON plugin_wxbot_reply_queue (tenant_id, command_id) "
        "WHERE command_id <> ''",
        "CREATE INDEX IF NOT EXISTS idx_wxbot_reply_queue_due "
        "ON plugin_wxbot_reply_queue (tenant_id, status, not_before, expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_wxbot_reply_queue_claim "
        "ON plugin_wxbot_reply_queue "
        "(tenant_id, status, claim_until, not_before, created_at, id)",
        """
        CREATE TABLE IF NOT EXISTS plugin_wxbot_interaction_cursor (
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            latest_message_id VARCHAR(128) NOT NULL DEFAULT '',
            latest_received_at TIMESTAMP NOT NULL DEFAULT NOW(),
            last_replied_message_id VARCHAR(128) NOT NULL DEFAULT '',
            last_reply_at TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tenant_id, session_id)
        )
        """,
    )


def _event_schema() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_wxbot_user_bans (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            user_wxid VARCHAR(256) NOT NULL,
            user_name VARCHAR(256) DEFAULT '',
            reason TEXT DEFAULT '',
            created_by VARCHAR(256) DEFAULT '',
            expires_at TIMESTAMP,
            revoked_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_wxbot_user_bans_lookup "
        "ON plugin_wxbot_user_bans "
        "(tenant_id, session_id, user_wxid, revoked_at, expires_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_wxbot_user_bans_active_unique "
        "ON plugin_wxbot_user_bans (tenant_id, session_id, user_wxid) "
        "WHERE revoked_at IS NULL",
        """
        CREATE TABLE IF NOT EXISTS plugin_wxbot_member_events (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            sdk_event_id BIGINT NOT NULL,
            event_type VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            session_name VARCHAR(256) DEFAULT '',
            entity_wxid VARCHAR(256) DEFAULT '',
            entity_name VARCHAR(256) DEFAULT '',
            payload_json TEXT DEFAULT '{}',
            created_ts BIGINT NOT NULL,
            received_at TIMESTAMP DEFAULT NOW()
        )
        """,
        "ALTER TABLE plugin_wxbot_member_events DROP CONSTRAINT IF EXISTS "
        "plugin_wxbot_member_events_sdk_event_id_key",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_wxbot_member_events_tenant_sdk_event_id_unique "
        "ON plugin_wxbot_member_events (tenant_id, sdk_event_id)",
        "CREATE INDEX IF NOT EXISTS idx_wxbot_member_events_tenant_created "
        "ON plugin_wxbot_member_events (tenant_id, created_ts DESC, id DESC)",
        """
        CREATE TABLE IF NOT EXISTS plugin_wxbot_media_ready_events (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            sdk_event_id BIGINT NOT NULL,
            event_type VARCHAR(64) NOT NULL,
            stream_event_id VARCHAR(128) DEFAULT '',
            message_id VARCHAR(128) DEFAULT '',
            session_id VARCHAR(256) NOT NULL,
            session_name VARCHAR(256) DEFAULT '',
            sender_wxid VARCHAR(256) DEFAULT '',
            sender_name VARCHAR(256) DEFAULT '',
            msg_type VARCHAR(32) DEFAULT '',
            media_type VARCHAR(32) DEFAULT '',
            media_path TEXT DEFAULT '',
            media_url TEXT DEFAULT '',
            payload_json TEXT DEFAULT '{}',
            created_ts BIGINT NOT NULL,
            received_at TIMESTAMP DEFAULT NOW()
        )
        """,
        "ALTER TABLE plugin_wxbot_media_ready_events DROP CONSTRAINT IF EXISTS "
        "plugin_wxbot_media_ready_events_sdk_event_id_key",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_wxbot_media_ready_events_tenant_sdk_event_id_unique "
        "ON plugin_wxbot_media_ready_events (tenant_id, sdk_event_id)",
        "CREATE INDEX IF NOT EXISTS idx_wxbot_media_ready_events_tenant_created "
        "ON plugin_wxbot_media_ready_events (tenant_id, created_ts DESC, id DESC)",
    )


def _group_context_schema() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_wxbot_group_observations (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            message_id VARCHAR(128) NOT NULL,
            session_name VARCHAR(256) DEFAULT '',
            sender_wxid VARCHAR(256) DEFAULT '',
            sender_name VARCHAR(256) DEFAULT '',
            msg_type VARCHAR(32) DEFAULT 'text',
            content TEXT DEFAULT '',
            mentioned_me BOOLEAN NOT NULL DEFAULT FALSE,
            bot_addressed BOOLEAN NOT NULL DEFAULT FALSE,
            is_self_sent BOOLEAN NOT NULL DEFAULT FALSE,
            occurred_ts BIGINT NOT NULL DEFAULT 0,
            metadata_json TEXT DEFAULT '{}',
            received_at TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, session_id, message_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_wxbot_group_observations_recent "
        "ON plugin_wxbot_group_observations (tenant_id, session_id, id DESC)",
        """
        CREATE TABLE IF NOT EXISTS plugin_wxbot_group_summary_state (
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            session_name VARCHAR(256) DEFAULT '',
            summary_text TEXT DEFAULT '',
            summary_json TEXT DEFAULT '{}',
            last_observation_id BIGINT NOT NULL DEFAULT 0,
            last_message_id VARCHAR(128) DEFAULT '',
            message_count INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tenant_id, session_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_wxbot_group_summary_jobs (
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            session_name VARCHAR(256) DEFAULT '',
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            requested_through_observation_id BIGINT NOT NULL DEFAULT 0,
            claimed_through_observation_id BIGINT NOT NULL DEFAULT 0,
            claimed_by VARCHAR(128) DEFAULT '',
            claim_token VARCHAR(64) DEFAULT '',
            claim_expires_at TIMESTAMP,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMP NOT NULL DEFAULT NOW(),
            error TEXT DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            PRIMARY KEY (tenant_id, session_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_wxbot_group_summary_jobs_claim "
        "ON plugin_wxbot_group_summary_jobs "
        "(status, next_attempt_at, claim_expires_at)",
    )


def _policy_schema() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_wxbot_tenant_policy (
            tenant_id VARCHAR(64) PRIMARY KEY,
            private_reply_mode VARCHAR(32) NOT NULL DEFAULT 'all',
            group_reply_mode VARCHAR(32) NOT NULL DEFAULT 'off',
            group_reply_mention_sender BOOLEAN NOT NULL DEFAULT FALSE,
            trigger_keywords_text TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_wxbot_session_policy (
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            reply_mode VARCHAR(32) NOT NULL DEFAULT 'inherit',
            mention_sender_mode VARCHAR(16) NOT NULL DEFAULT 'inherit',
            trigger_keywords_text TEXT DEFAULT '',
            participation_policy_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (tenant_id, session_id)
        )
        """,
        "ALTER TABLE plugin_wxbot_tenant_policy ADD COLUMN IF NOT EXISTS "
        "group_reply_mention_sender BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE plugin_wxbot_tenant_policy "
        "ALTER COLUMN group_reply_mention_sender SET DEFAULT FALSE",
        "ALTER TABLE plugin_wxbot_session_policy ADD COLUMN IF NOT EXISTS "
        "mention_sender_mode VARCHAR(16) NOT NULL DEFAULT 'inherit'",
        "ALTER TABLE plugin_wxbot_session_policy ADD COLUMN IF NOT EXISTS "
        "participation_policy_json JSONB NOT NULL DEFAULT '{}'::jsonb",
    )


def _report_schema() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_wxbot_report_subscriptions (
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            session_name VARCHAR(256) DEFAULT '',
            daily_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            weekly_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            monthly_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            daily_hour INTEGER NOT NULL DEFAULT 9,
            weekly_day INTEGER NOT NULL DEFAULT 1,
            weekly_hour INTEGER NOT NULL DEFAULT 9,
            monthly_day INTEGER NOT NULL DEFAULT 1,
            tz VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (tenant_id, session_id)
        )
        """,
        "ALTER TABLE plugin_wxbot_report_subscriptions ADD COLUMN IF NOT EXISTS "
        "weekly_enabled BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE plugin_wxbot_report_subscriptions ADD COLUMN IF NOT EXISTS "
        "weekly_day INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE plugin_wxbot_report_subscriptions ADD COLUMN IF NOT EXISTS "
        "weekly_hour INTEGER NOT NULL DEFAULT 9",
        "CREATE INDEX IF NOT EXISTS idx_wxbot_report_subscriptions_enabled "
        "ON plugin_wxbot_report_subscriptions (tenant_id, updated_at DESC)",
        """
        CREATE TABLE IF NOT EXISTS plugin_wxbot_report_jobs (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            session_name VARCHAR(256) DEFAULT '',
            report_type VARCHAR(16) NOT NULL,
            period_key VARCHAR(32) NOT NULL,
            period_label VARCHAR(64) DEFAULT '',
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            current_stage VARCHAR(32) NOT NULL DEFAULT 'queued',
            msg_count INTEGER DEFAULT 0,
            result_text TEXT DEFAULT '',
            report_json TEXT DEFAULT '',
            delivery_status VARCHAR(16) NOT NULL DEFAULT 'pending',
            delivery_error TEXT DEFAULT '',
            delivered_at TIMESTAMP,
            error TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            completed_at TIMESTAMP,
            UNIQUE (tenant_id, session_id, report_type, period_key)
        )
        """,
        "ALTER TABLE plugin_wxbot_report_jobs ADD COLUMN IF NOT EXISTS "
        "delivery_status VARCHAR(16) NOT NULL DEFAULT 'pending'",
        "ALTER TABLE plugin_wxbot_report_jobs ADD COLUMN IF NOT EXISTS "
        "delivery_error TEXT DEFAULT ''",
        "ALTER TABLE plugin_wxbot_report_jobs ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMP",
        "CREATE INDEX IF NOT EXISTS idx_wxbot_report_jobs_scope "
        "ON plugin_wxbot_report_jobs "
        "(tenant_id, session_id, report_type, period_key)",
        """
        CREATE TABLE IF NOT EXISTS plugin_wxbot_self_review_subscriptions (
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            session_name VARCHAR(256) DEFAULT '',
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            daily_hour INTEGER NOT NULL DEFAULT 23,
            tz VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
            focus_mode VARCHAR(32) NOT NULL DEFAULT 'bot_interactions',
            auto_create_kb_doc BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (tenant_id, session_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_wxbot_self_review_subscriptions_enabled "
        "ON plugin_wxbot_self_review_subscriptions (tenant_id, updated_at DESC)",
        """
        CREATE TABLE IF NOT EXISTS plugin_wxbot_self_review_jobs (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            session_name VARCHAR(256) DEFAULT '',
            period_key VARCHAR(32) NOT NULL,
            period_label VARCHAR(64) DEFAULT '',
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            current_stage VARCHAR(32) NOT NULL DEFAULT 'queued',
            msg_count INTEGER DEFAULT 0,
            result_text TEXT DEFAULT '',
            review_json TEXT DEFAULT '',
            kb_doc_id BIGINT,
            kb_doc_title VARCHAR(512) DEFAULT '',
            error TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            completed_at TIMESTAMP,
            UNIQUE (tenant_id, session_id, period_key)
        )
        """,
        "ALTER TABLE plugin_wxbot_self_review_jobs "
        "ADD COLUMN IF NOT EXISTS kb_doc_id BIGINT",
        "ALTER TABLE plugin_wxbot_self_review_jobs "
        "ADD COLUMN IF NOT EXISTS kb_doc_title VARCHAR(512) DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_wxbot_self_review_jobs_scope "
        "ON plugin_wxbot_self_review_jobs (tenant_id, session_id, period_key)",
    )
