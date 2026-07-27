"""runtime hardening for group authorization, policies and historical privacy

Revision ID: 0011_runtime_hardening
Revises: 0010_memory_extraction_job_result
Create Date: 2026-07-13

The knowledge re-chunk/re-embed operation intentionally lives in the bounded
``POST /v1/admin/kb/reindex`` maintenance API because migrations must not call
an external embedding provider. This migration performs the relational and
privacy-safe portions synchronously and idempotently.
"""
from __future__ import annotations

from alembic import op

revision = "0011_runtime_hardening"
down_revision = "0010_memory_extraction_job_result"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS plugin_wxbot_interaction_cursor (
            tenant_id              VARCHAR(64) NOT NULL,
            session_id             VARCHAR(256) NOT NULL,
            latest_message_id       VARCHAR(128) NOT NULL DEFAULT '',
            latest_received_at      TIMESTAMP NOT NULL DEFAULT NOW(),
            last_replied_message_id VARCHAR(128) NOT NULL DEFAULT '',
            last_reply_at           TIMESTAMP,
            burst_count             INTEGER NOT NULL DEFAULT 1,
            burst_started_at        TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMP NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tenant_id, session_id)
        )
    """)
    op.execute("""
        ALTER TABLE plugin_wxbot_interaction_cursor
            ADD COLUMN IF NOT EXISTS burst_count INTEGER NOT NULL DEFAULT 1
    """)
    op.execute("""
        ALTER TABLE plugin_wxbot_interaction_cursor
            ADD COLUMN IF NOT EXISTS burst_started_at TIMESTAMP NOT NULL DEFAULT NOW()
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS plugin_wxbot_group_membership (
            tenant_id       VARCHAR(64) NOT NULL,
            session_id      VARCHAR(256) NOT NULL,
            user_wxid       VARCHAR(256) NOT NULL,
            user_name       VARCHAR(256) DEFAULT '',
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            joined_at       TIMESTAMP,
            left_at         TIMESTAMP,
            last_event_id   BIGINT,
            updated_at      TIMESTAMP NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tenant_id, session_id, user_wxid)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_wxbot_group_membership_active
            ON plugin_wxbot_group_membership
            (tenant_id, session_id, is_active, user_wxid)
    """)
    op.execute("""
        DO $$
        BEGIN
            IF to_regclass('public.plugin_wxbot_session_policy') IS NOT NULL THEN
                ALTER TABLE plugin_wxbot_session_policy
                    ADD COLUMN IF NOT EXISTS reply_cooldown_seconds DOUBLE PRECISION NULL;
                ALTER TABLE plugin_wxbot_session_policy
                    ADD COLUMN IF NOT EXISTS coalesce_window_ms INTEGER NULL;
                ALTER TABLE plugin_wxbot_session_policy
                    ADD COLUMN IF NOT EXISTS adaptive_cooldown_enabled BOOLEAN NULL;
            END IF;
        END $$;
    """)

    # Reconstruct the latest known membership state from historical join/left
    # events. Active speakers are additionally upserted by the bridge at
    # runtime, covering groups whose original join event predates retention.
    op.execute("""
        DO $$
        BEGIN
            IF to_regclass('public.plugin_wxbot_member_events') IS NOT NULL THEN
                INSERT INTO plugin_wxbot_group_membership (
                    tenant_id, session_id, user_wxid, user_name, is_active,
                    joined_at, left_at, last_event_id, updated_at
                )
                SELECT DISTINCT ON (tenant_id, session_id, entity_wxid)
                    tenant_id,
                    session_id,
                    entity_wxid,
                    entity_name,
                    NOT (
                        LOWER(event_type) LIKE '%.left'
                        OR LOWER(event_type) LIKE '%.removed'
                    ),
                    CASE WHEN LOWER(event_type) LIKE '%.joined'
                        OR LOWER(event_type) LIKE '%.added'
                        THEN TO_TIMESTAMP(created_ts) ELSE NULL END,
                    CASE WHEN LOWER(event_type) LIKE '%.left'
                        OR LOWER(event_type) LIKE '%.removed'
                        THEN TO_TIMESTAMP(created_ts) ELSE NULL END,
                    sdk_event_id,
                    NOW()
                FROM plugin_wxbot_member_events
                WHERE session_id LIKE '%@chatroom' AND entity_wxid <> ''
                ORDER BY tenant_id, session_id, entity_wxid, created_ts DESC, sdk_event_id DESC
                ON CONFLICT (tenant_id, session_id, user_wxid) DO UPDATE SET
                    user_name = EXCLUDED.user_name,
                    is_active = EXCLUDED.is_active,
                    joined_at = COALESCE(EXCLUDED.joined_at,
                        plugin_wxbot_group_membership.joined_at),
                    left_at = EXCLUDED.left_at,
                    last_event_id = EXCLUDED.last_event_id,
                    updated_at = NOW();
            END IF;
        END $$;
    """)

    # Remove historical actor-private state accidentally persisted on a shared
    # group Session row. This mirrors the new write-path privacy invariant.
    op.execute("""
        DO $$
        BEGIN
            IF to_regclass('public.sessions') IS NOT NULL THEN
                UPDATE sessions
                SET variables = ((COALESCE(variables::jsonb, '{}'::jsonb)
                        - 'user_memory') - 'group_memory')::json,
                    pii_map = '{}'::json,
                    user_id = session_id
                WHERE session_id LIKE '%@chatroom';
            END IF;
        END $$;
    """)

    # Legacy auto-extracted group facts without an acceptance decision are not
    # trusted. Move them into the existing review queue instead of deleting
    # evidence that an administrator may need while correcting history.
    op.execute("""
        DO $$
        BEGIN
            IF to_regclass('public.plugin_memory_item') IS NOT NULL THEN
                UPDATE plugin_memory_item
                SET value_json = jsonb_set(
                        COALESCE(NULLIF(value_json, '')::jsonb, '{}'::jsonb),
                        '{acceptance}',
                        jsonb_build_object(
                            'status', 'needs_review',
                            'reason', 'historical_group_memory_migration',
                            'reviewed_at', NULL,
                            'reviewed_by', ''
                        ),
                        TRUE
                    )::text,
                    status = 'pending',
                    updated_at = NOW()
                WHERE session_id LIKE '%@chatroom'
                  AND source_type <> 'manual'
                  AND COALESCE(NULLIF(value_json, '')::jsonb, '{}'::jsonb)
                        -> 'acceptance' IS NULL
                  AND deleted_at IS NULL;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    # Historical privacy cleanup and review classification are intentionally
    # irreversible. Only the additive runtime schema is removed.
    op.execute("""
        DO $$
        BEGIN
            IF to_regclass('public.plugin_wxbot_session_policy') IS NOT NULL THEN
                ALTER TABLE plugin_wxbot_session_policy
                    DROP COLUMN IF EXISTS adaptive_cooldown_enabled;
                ALTER TABLE plugin_wxbot_session_policy
                    DROP COLUMN IF EXISTS coalesce_window_ms;
                ALTER TABLE plugin_wxbot_session_policy
                    DROP COLUMN IF EXISTS reply_cooldown_seconds;
            END IF;
        END $$
    """)
    op.execute("DROP TABLE IF EXISTS plugin_wxbot_group_membership")
    op.execute("""
        ALTER TABLE IF EXISTS plugin_wxbot_interaction_cursor
            DROP COLUMN IF EXISTS burst_started_at
    """)
    op.execute("""
        ALTER TABLE IF EXISTS plugin_wxbot_interaction_cursor
            DROP COLUMN IF EXISTS burst_count
    """)
