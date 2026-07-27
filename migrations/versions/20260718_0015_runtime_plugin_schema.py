"""adopt all non-wxbot runtime-owned plugin schemas

Revision ID: 0015_runtime_plugin_schema
Revises: 0014_session_tenant_scope_contract
Create Date: 2026-07-18

This migration is intentionally idempotent: existing deployments may already
have some or all of these objects from legacy startup bootstraps, while a fresh
Alembic installation has none of several plugin tables.  Every table is
created before compatibility ALTER statements are applied.
"""

from __future__ import annotations

from alembic import op

revision = "0015_runtime_plugin_schema"
down_revision = "0014_session_tenant_scope_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _agent_schema()
    _plugin_state_schema()
    _commands_schema()
    _credits_schema()
    _draw_schema()
    _group_activity_schema()
    _memory_schema()
    _moderation_schema()
    _persona_schema()
    _repeater_schema()
    _tibo_reset_schema()


def downgrade() -> None:
    # This is an adoption migration.  Objects may contain data created before
    # Alembic owned them, so downgrade deliberately preserves them.
    pass


def _execute(*statements: str) -> None:
    for statement in statements:
        op.execute(statement)


def _agent_schema() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_agent_session_policy (
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            scope VARCHAR(64) NOT NULL DEFAULT 'group_info',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            allowed_tools_json TEXT NOT NULL DEFAULT '[]',
            updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (tenant_id, session_id, scope)
        )
        """,
        "ALTER TABLE plugin_agent_session_policy ADD COLUMN IF NOT EXISTS scope VARCHAR(64)",
        "UPDATE plugin_agent_session_policy SET scope = 'group_info' "
        "WHERE scope IS NULL OR scope = ''",
        "ALTER TABLE plugin_agent_session_policy ALTER COLUMN scope SET DEFAULT 'group_info'",
        "ALTER TABLE plugin_agent_session_policy ALTER COLUMN scope SET NOT NULL",
        "ALTER TABLE plugin_agent_session_policy "
        "DROP CONSTRAINT IF EXISTS plugin_agent_session_policy_pkey",
        "ALTER TABLE plugin_agent_session_policy "
        "ADD CONSTRAINT plugin_agent_session_policy_pkey "
        "PRIMARY KEY (tenant_id, session_id, scope)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_plugin_agent_session_policy_scope "
        "ON plugin_agent_session_policy (tenant_id, session_id, scope)",
        """
        CREATE TABLE IF NOT EXISTS plugin_agent_tool_audit (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            user_id VARCHAR(256) DEFAULT '',
            channel VARCHAR(32) DEFAULT '',
            scope VARCHAR(64) DEFAULT '',
            tool_name VARCHAR(64) NOT NULL,
            tool_args_json TEXT NOT NULL DEFAULT '{}',
            tool_result_json TEXT NOT NULL DEFAULT '',
            tool_error TEXT DEFAULT '',
            latency_ms INTEGER DEFAULT 0,
            trace_id VARCHAR(128) DEFAULT '',
            final_reply_text TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_plugin_agent_tool_audit_scope "
        "ON plugin_agent_tool_audit (tenant_id, session_id, created_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_plugin_agent_tool_audit_trace "
        "ON plugin_agent_tool_audit (trace_id)",
    )


def _plugin_state_schema() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_state (
            plugin_name VARCHAR(128) PRIMARY KEY,
            version VARCHAR(64) NOT NULL DEFAULT '',
            source VARCHAR(64) NOT NULL DEFAULT 'builtin',
            installed BOOLEAN NOT NULL DEFAULT TRUE,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            system BOOLEAN NOT NULL DEFAULT FALSE,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            restart_required BOOLEAN NOT NULL DEFAULT FALSE,
            last_error TEXT NOT NULL DEFAULT '',
            metadata_json JSONB NOT NULL DEFAULT '{}',
            installed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_events (
            id BIGSERIAL PRIMARY KEY,
            plugin_name VARCHAR(128) NOT NULL,
            event_type VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'ok',
            actor_id TEXT NOT NULL DEFAULT '',
            actor_type VARCHAR(32) NOT NULL DEFAULT 'admin',
            request_id VARCHAR(128) NOT NULL DEFAULT '',
            ip_address VARCHAR(64) NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            metadata_json JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_scope_state (
            tenant_id VARCHAR(128) NOT NULL,
            session_id VARCHAR(256) NOT NULL DEFAULT '',
            plugin_name VARCHAR(128) NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            config_json JSONB NOT NULL DEFAULT '{}',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tenant_id, session_id, plugin_name)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_plugin_events_plugin_created "
        "ON plugin_events (plugin_name, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_plugin_events_type_created "
        "ON plugin_events (event_type, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_plugin_scope_state_plugin "
        "ON plugin_scope_state (plugin_name)",
    )


def _commands_schema() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_command_center_config (
            tenant_id VARCHAR(64) PRIMARY KEY,
            admin_user_ids_text TEXT DEFAULT '',
            user_commands_text TEXT DEFAULT '',
            admin_commands_text TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """,
        "ALTER TABLE plugin_command_center_config "
        "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()",
    )


def _credits_schema() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_credits_config (
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            enabled BOOLEAN DEFAULT FALSE,
            credit_name VARCHAR(64) DEFAULT '积分',
            cost_per_chat INTEGER DEFAULT 0,
            command_costs_text TEXT DEFAULT '',
            draw_quality_costs_text TEXT DEFAULT 'low=5\nmedium=10\nhigh=20',
            amap_search_credit_cost INTEGER DEFAULT 2,
            amap_map_credit_cost INTEGER DEFAULT 8,
            amap_route_map_credit_cost INTEGER DEFAULT 12,
            initial_credits INTEGER DEFAULT 100,
            daily_checkin INTEGER DEFAULT 10,
            streak_bonus INTEGER DEFAULT 5,
            streak_cap INTEGER DEFAULT 50,
            checkin_mode INTEGER NOT NULL DEFAULT 1,
            admin_user_ids_text TEXT DEFAULT '',
            user_commands_text TEXT DEFAULT '',
            admin_commands_text TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (tenant_id, session_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_credits_balance (
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            user_id VARCHAR(128) NOT NULL,
            display_name VARCHAR(128) DEFAULT '',
            credits INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (tenant_id, session_id, user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_credits_ledger (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            user_id VARCHAR(128) NOT NULL,
            delta INTEGER NOT NULL,
            reason VARCHAR(64) NOT NULL,
            actor VARCHAR(128) DEFAULT '',
            reference VARCHAR(256) DEFAULT '',
            idempotency_key VARCHAR(128) NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_credits_checkin (
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            user_id VARCHAR(128) NOT NULL,
            checkin_date DATE NOT NULL,
            streak INTEGER DEFAULT 1,
            reward INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (tenant_id, session_id, user_id, checkin_date)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_credits_reservation (
            reservation_id VARCHAR(64) PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            user_id VARCHAR(128) NOT NULL,
            amount INTEGER NOT NULL,
            reason VARCHAR(64) NOT NULL,
            reference VARCHAR(256) DEFAULT '',
            idempotency_key VARCHAR(256) NOT NULL DEFAULT '',
            status VARCHAR(16) NOT NULL DEFAULT 'reserved',
            captured_amount INTEGER NULL,
            metadata_json TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            captured_at TIMESTAMP NULL,
            released_at TIMESTAMP NULL
        )
        """,
        "ALTER TABLE plugin_credits_config ALTER COLUMN session_id TYPE VARCHAR(256)",
        "ALTER TABLE plugin_credits_balance ALTER COLUMN session_id TYPE VARCHAR(256)",
        "ALTER TABLE plugin_credits_ledger ALTER COLUMN session_id TYPE VARCHAR(256)",
        "ALTER TABLE plugin_credits_checkin ALTER COLUMN session_id TYPE VARCHAR(256)",
        "ALTER TABLE plugin_credits_reservation ALTER COLUMN session_id TYPE VARCHAR(256)",
        "ALTER TABLE plugin_credits_ledger "
        "ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128) NOT NULL DEFAULT ''",
        "ALTER TABLE plugin_credits_reservation "
        "ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(256) NOT NULL DEFAULT ''",
        "ALTER TABLE plugin_credits_reservation "
        "ADD COLUMN IF NOT EXISTS captured_amount INTEGER NULL",
        "UPDATE plugin_credits_reservation SET captured_amount = amount "
        "WHERE status = 'captured' AND captured_amount IS NULL",
        "ALTER TABLE plugin_credits_config "
        "ADD COLUMN IF NOT EXISTS checkin_mode INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE plugin_credits_config "
        "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()",
        "ALTER TABLE plugin_credits_config "
        "ADD COLUMN IF NOT EXISTS admin_user_ids_text TEXT DEFAULT ''",
        "ALTER TABLE plugin_credits_config "
        "ADD COLUMN IF NOT EXISTS command_costs_text TEXT DEFAULT ''",
        "ALTER TABLE plugin_credits_config ADD COLUMN IF NOT EXISTS "
        "draw_quality_costs_text TEXT DEFAULT 'low=5\nmedium=10\nhigh=20'",
        "ALTER TABLE plugin_credits_config "
        "ADD COLUMN IF NOT EXISTS amap_search_credit_cost INTEGER DEFAULT 2",
        "ALTER TABLE plugin_credits_config "
        "ADD COLUMN IF NOT EXISTS amap_map_credit_cost INTEGER DEFAULT 8",
        "ALTER TABLE plugin_credits_config ADD COLUMN IF NOT EXISTS "
        "amap_route_map_credit_cost INTEGER DEFAULT 12",
        "ALTER TABLE plugin_credits_config "
        "ADD COLUMN IF NOT EXISTS user_commands_text TEXT DEFAULT ''",
        "ALTER TABLE plugin_credits_config "
        "ADD COLUMN IF NOT EXISTS admin_commands_text TEXT DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_plugin_credits_balance_session_rank "
        "ON plugin_credits_balance "
        "(tenant_id, session_id, credits DESC, updated_at DESC, user_id)",
        "CREATE INDEX IF NOT EXISTS idx_plugin_credits_ledger_session_created "
        "ON plugin_credits_ledger "
        "(tenant_id, session_id, created_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_plugin_credits_ledger_session_user "
        "ON plugin_credits_ledger "
        "(tenant_id, session_id, user_id, created_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_plugin_credits_checkin_session_date "
        "ON plugin_credits_checkin "
        "(tenant_id, session_id, checkin_date DESC, user_id)",
        "CREATE INDEX IF NOT EXISTS idx_plugin_credits_checkin_session_user "
        "ON plugin_credits_checkin "
        "(tenant_id, session_id, user_id, checkin_date DESC)",
        "CREATE INDEX IF NOT EXISTS idx_plugin_credits_reservation_subject_status "
        "ON plugin_credits_reservation "
        "(tenant_id, session_id, user_id, status, created_at DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_plugin_credits_ledger_idempotency "
        "ON plugin_credits_ledger (idempotency_key) WHERE idempotency_key <> ''",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_plugin_credits_reservation_idempotency "
        "ON plugin_credits_reservation "
        "(tenant_id, session_id, user_id, idempotency_key) WHERE idempotency_key <> ''",
    )


def _draw_schema() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_draw_task (
            task_id VARCHAR(64) PRIMARY KEY,
            request_id VARCHAR(128) DEFAULT '',
            trace_id VARCHAR(128) DEFAULT '',
            command_type VARCHAR(32) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'queued',
            tenant_id VARCHAR(64) DEFAULT '',
            channel VARCHAR(32) DEFAULT '',
            source_key TEXT DEFAULT '',
            chat_id VARCHAR(256) DEFAULT '',
            session_id VARCHAR(256) DEFAULT '',
            group_id VARCHAR(256) DEFAULT '',
            user_id VARCHAR(256) DEFAULT '',
            requester VARCHAR(256) DEFAULT '',
            requester_display_name VARCHAR(256) DEFAULT '',
            original_message_id VARCHAR(128) DEFAULT '',
            callback_target_json TEXT DEFAULT '{}',
            callback_reply_to_message_id VARCHAR(128) DEFAULT '',
            source_message_json TEXT DEFAULT '{}',
            prompt TEXT DEFAULT '',
            quality VARCHAR(16) DEFAULT 'low',
            size VARCHAR(32) DEFAULT '',
            source_image_json TEXT DEFAULT '{}',
            result_image_id VARCHAR(128) DEFAULT '',
            result_local_path TEXT DEFAULT '',
            result_file_name TEXT DEFAULT '',
            result_media_type VARCHAR(128) DEFAULT '',
            result_public_path TEXT DEFAULT '',
            result_source_url TEXT DEFAULT '',
            error_code VARCHAR(128) DEFAULT '',
            error_message TEXT DEFAULT '',
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_run_at TIMESTAMPTZ,
            locked_until TIMESTAMPTZ,
            locked_by VARCHAR(128) DEFAULT '',
            callback_sent BOOLEAN NOT NULL DEFAULT FALSE,
            callback_error TEXT DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            heartbeat_at TIMESTAMPTZ
        )
        """,
        "ALTER TABLE plugin_draw_task "
        "ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE plugin_draw_task ADD COLUMN IF NOT EXISTS next_run_at TIMESTAMPTZ",
        "ALTER TABLE plugin_draw_task ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ",
        "ALTER TABLE plugin_draw_task ADD COLUMN IF NOT EXISTS locked_by VARCHAR(128) DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_draw_task_status_heartbeat "
        "ON plugin_draw_task (status, heartbeat_at, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_draw_task_tenant_created "
        "ON plugin_draw_task (tenant_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_draw_task_queue_due "
        "ON plugin_draw_task (status, next_run_at, locked_until)",
    )


def _group_activity_schema() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_group_activity_config (
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            session_name VARCHAR(256) DEFAULT '',
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            active_start VARCHAR(5) NOT NULL DEFAULT '08:00',
            active_end VARCHAR(5) NOT NULL DEFAULT '17:00',
            quiet_start VARCHAR(5) NOT NULL DEFAULT '22:00',
            quiet_end VARCHAR(5) NOT NULL DEFAULT '08:00',
            timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
            idle_minutes INTEGER NOT NULL DEFAULT 60,
            lookback_minutes INTEGER NOT NULL DEFAULT 120,
            min_send_interval_minutes INTEGER NOT NULL DEFAULT 180,
            max_per_day INTEGER NOT NULL DEFAULT 1,
            topic_repeat_window_minutes INTEGER NOT NULL DEFAULT 1440,
            llm_model_tier VARCHAR(32) NOT NULL DEFAULT 'tier-2',
            temperature DOUBLE PRECISION NOT NULL DEFAULT 0.9,
            agent_tool_scope VARCHAR(64) NOT NULL DEFAULT 'group_info',
            updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (tenant_id, session_id)
        )
        """,
        "ALTER TABLE plugin_group_activity_config ADD COLUMN IF NOT EXISTS "
        "agent_tool_scope VARCHAR(64) NOT NULL DEFAULT 'group_info'",
        "ALTER TABLE plugin_group_activity_config "
        "ADD COLUMN IF NOT EXISTS quiet_start VARCHAR(5) NOT NULL DEFAULT '22:00', "
        "ADD COLUMN IF NOT EXISTS quiet_end VARCHAR(5) NOT NULL DEFAULT '08:00', "
        "ADD COLUMN IF NOT EXISTS topic_repeat_window_minutes "
        "INTEGER NOT NULL DEFAULT 1440",
        "ALTER TABLE plugin_group_activity_config ALTER COLUMN idle_minutes SET DEFAULT 60",
        "ALTER TABLE plugin_group_activity_config "
        "ALTER COLUMN lookback_minutes SET DEFAULT 120, "
        "ALTER COLUMN min_send_interval_minutes SET DEFAULT 180, "
        "ALTER COLUMN max_per_day SET DEFAULT 1",
        """
        CREATE TABLE IF NOT EXISTS plugin_group_activity_event (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            session_name VARCHAR(256) DEFAULT '',
            slot_key VARCHAR(64) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            last_user_message_ts BIGINT DEFAULT 0,
            message_count INTEGER DEFAULT 0,
            prompt_hash VARCHAR(64) DEFAULT '',
            generated_text TEXT DEFAULT '',
            reply_queue_id BIGINT,
            command_id VARCHAR(256) DEFAULT '',
            trace_id VARCHAR(128) DEFAULT '',
            reason_code VARCHAR(64) DEFAULT '',
            error TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            completed_at TIMESTAMP,
            UNIQUE (tenant_id, session_id, slot_key)
        )
        """,
        "ALTER TABLE plugin_group_activity_event "
        "ADD COLUMN IF NOT EXISTS reason_code VARCHAR(64) DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_group_activity_event_scope_created "
        "ON plugin_group_activity_event (tenant_id, session_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_group_activity_event_status_updated "
        "ON plugin_group_activity_event (tenant_id, status, updated_at DESC)",
    )


def _memory_schema() -> None:
    _memory_profile_tables()
    _memory_item_tables()
    _memory_graph_tables()
    _memory_indexes_and_compatibility()
    _memory_legacy_data()


def _memory_profile_tables() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_memory_identity_profile (
            tenant_id VARCHAR(64) NOT NULL,
            channel VARCHAR(32) NOT NULL,
            source_key VARCHAR(128) NOT NULL DEFAULT '*',
            user_id VARCHAR(128) NOT NULL,
            long_term_memory TEXT DEFAULT '',
            manual_notes TEXT DEFAULT '',
            long_term_items_json TEXT DEFAULT '[]',
            message_count INTEGER DEFAULT 0,
            imported_message_count INTEGER DEFAULT 0,
            last_session_id VARCHAR(256) DEFAULT '',
            last_seen_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (tenant_id, channel, source_key, user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_memory_session_profile (
            tenant_id VARCHAR(64) NOT NULL,
            channel VARCHAR(32) NOT NULL,
            source_key VARCHAR(128) NOT NULL DEFAULT '*',
            session_id VARCHAR(256) NOT NULL,
            user_id VARCHAR(128) NOT NULL,
            short_term_memory TEXT DEFAULT '',
            manual_notes TEXT DEFAULT '',
            short_term_items_json TEXT DEFAULT '[]',
            session_summary TEXT DEFAULT '',
            open_items_json TEXT DEFAULT '[]',
            decisions_json TEXT DEFAULT '[]',
            recent_turns_json TEXT DEFAULT '[]',
            last_compacted_at TIMESTAMP NULL,
            summary_version INTEGER DEFAULT 1,
            message_count INTEGER DEFAULT 0,
            imported_message_count INTEGER DEFAULT 0,
            last_seen_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (tenant_id, channel, source_key, session_id, user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_memory_event (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            channel VARCHAR(32) NOT NULL,
            source_key VARCHAR(128) NOT NULL DEFAULT '*',
            user_id VARCHAR(128) NOT NULL,
            session_id VARCHAR(256) DEFAULT '',
            user_text TEXT DEFAULT '',
            assistant_text TEXT DEFAULT '',
            trace_id VARCHAR(128) DEFAULT '',
            event_key VARCHAR(128) NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
    )


def _memory_item_tables() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_memory_item (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            channel VARCHAR(32) NOT NULL,
            source_key VARCHAR(128) NOT NULL DEFAULT '*',
            user_id VARCHAR(128) NOT NULL,
            session_id VARCHAR(256) DEFAULT '',
            scope_type VARCHAR(32) NOT NULL DEFAULT 'identity',
            source_type VARCHAR(32) NOT NULL DEFAULT 'auto',
            memory_type VARCHAR(32) NOT NULL DEFAULT 'note',
            content TEXT NOT NULL DEFAULT '',
            value_json TEXT DEFAULT '{}',
            normalized_key VARCHAR(64) NOT NULL,
            confidence DOUBLE PRECISION DEFAULT 0.0,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            pinned BOOLEAN NOT NULL DEFAULT FALSE,
            priority INTEGER NOT NULL DEFAULT 0,
            sensitivity VARCHAR(32) NOT NULL DEFAULT 'normal',
            source_event_id BIGINT NULL,
            source_trace_id VARCHAR(128) DEFAULT '',
            original_text TEXT DEFAULT '',
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            first_seen_at TIMESTAMP DEFAULT NOW(),
            last_seen_at TIMESTAMP DEFAULT NOW(),
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            deleted_at TIMESTAMP NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_memory_acceptance_audit (
            id BIGSERIAL PRIMARY KEY,
            item_id BIGINT NOT NULL,
            tenant_id VARCHAR(64) NOT NULL,
            channel VARCHAR(32) NOT NULL,
            source_key VARCHAR(128) NOT NULL DEFAULT '*',
            user_id VARCHAR(128) NOT NULL,
            session_id VARCHAR(256) DEFAULT '',
            scope_type VARCHAR(32) DEFAULT '',
            source_type VARCHAR(32) DEFAULT '',
            action VARCHAR(32) NOT NULL,
            previous_status VARCHAR(32) DEFAULT '',
            new_status VARCHAR(32) DEFAULT '',
            previous_item_status VARCHAR(32) DEFAULT '',
            new_item_status VARCHAR(32) DEFAULT '',
            reviewed_by VARCHAR(128) DEFAULT '',
            actor VARCHAR(128) DEFAULT '',
            reason TEXT DEFAULT '',
            superseded_by_item_id BIGINT NULL,
            supersedes_item_id BIGINT NULL,
            reviewed_at TIMESTAMP DEFAULT NOW(),
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_memory_extraction_job (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            channel VARCHAR(32) NOT NULL,
            source_key VARCHAR(128) NOT NULL DEFAULT '*',
            user_id VARCHAR(128) NOT NULL,
            session_id VARCHAR(256) DEFAULT '',
            source_event_id BIGINT NULL,
            source_trace_id VARCHAR(128) DEFAULT '',
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            next_run_at TIMESTAMP NOT NULL DEFAULT NOW(),
            locked_until TIMESTAMP NULL,
            locked_by VARCHAR(128) DEFAULT '',
            last_error TEXT DEFAULT '',
            result_json TEXT DEFAULT '{}',
            idempotency_key VARCHAR(256) NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """,
    )


def _memory_graph_tables() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_memory_entity (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            channel VARCHAR(32) NOT NULL,
            source_key VARCHAR(128) NOT NULL DEFAULT '*',
            user_id VARCHAR(128) NOT NULL,
            entity_type VARCHAR(64) NOT NULL DEFAULT 'thing',
            name TEXT NOT NULL DEFAULT '',
            normalized_name VARCHAR(128) NOT NULL,
            aliases_json TEXT DEFAULT '[]',
            confidence DOUBLE PRECISION DEFAULT 0.0,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_memory_fact (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            channel VARCHAR(32) NOT NULL,
            source_key VARCHAR(128) NOT NULL DEFAULT '*',
            user_id VARCHAR(128) NOT NULL,
            subject_entity_id BIGINT NOT NULL,
            predicate VARCHAR(128) NOT NULL,
            object_entity_id BIGINT NULL,
            object_value TEXT DEFAULT '',
            memory_item_id BIGINT NOT NULL,
            source_event_id BIGINT NULL,
            confidence DOUBLE PRECISION DEFAULT 0.0,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            valid_at TIMESTAMP DEFAULT NOW(),
            invalid_at TIMESTAMP NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_memory_episode (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            channel VARCHAR(32) NOT NULL,
            source_key VARCHAR(128) NOT NULL DEFAULT '*',
            user_id VARCHAR(128) NOT NULL,
            session_id VARCHAR(256) DEFAULT '',
            title TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            event_ids_json TEXT DEFAULT '[]',
            memory_item_ids_json TEXT DEFAULT '[]',
            importance INTEGER NOT NULL DEFAULT 0,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """,
    )


def _memory_indexes_and_compatibility() -> None:
    _execute(
        "ALTER TABLE plugin_memory_event ADD COLUMN IF NOT EXISTS event_key VARCHAR(128) NULL",
        "ALTER TABLE plugin_memory_session_profile "
        "ADD COLUMN IF NOT EXISTS session_summary TEXT DEFAULT ''",
        "ALTER TABLE plugin_memory_session_profile "
        "ADD COLUMN IF NOT EXISTS open_items_json TEXT DEFAULT '[]'",
        "ALTER TABLE plugin_memory_session_profile "
        "ADD COLUMN IF NOT EXISTS decisions_json TEXT DEFAULT '[]'",
        "ALTER TABLE plugin_memory_session_profile "
        "ADD COLUMN IF NOT EXISTS recent_turns_json TEXT DEFAULT '[]'",
        "ALTER TABLE plugin_memory_session_profile "
        "ADD COLUMN IF NOT EXISTS last_compacted_at TIMESTAMP NULL",
        "ALTER TABLE plugin_memory_session_profile "
        "ADD COLUMN IF NOT EXISTS summary_version INTEGER DEFAULT 1",
        "ALTER TABLE plugin_memory_extraction_job "
        "ADD COLUMN IF NOT EXISTS result_json TEXT DEFAULT '{}'",
        "CREATE INDEX IF NOT EXISTS idx_memory_identity_lookup "
        "ON plugin_memory_identity_profile (tenant_id, channel, source_key, user_id)",
        "CREATE INDEX IF NOT EXISTS idx_memory_session_lookup "
        "ON plugin_memory_session_profile "
        "(tenant_id, channel, source_key, session_id, user_id)",
        "CREATE INDEX IF NOT EXISTS idx_memory_event_lookup "
        "ON plugin_memory_event "
        "(tenant_id, channel, source_key, user_id, created_at DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_event_key "
        "ON plugin_memory_event (event_key) WHERE event_key IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_memory_item_scope ON plugin_memory_item "
        "(tenant_id, channel, source_key, user_id, scope_type, session_id, "
        "status, updated_at DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_item_dedupe "
        "ON plugin_memory_item (tenant_id, channel, source_key, user_id, "
        "scope_type, session_id, source_type, normalized_key) "
        "WHERE deleted_at IS NULL",
        "CREATE INDEX IF NOT EXISTS idx_memory_acceptance_audit_item "
        "ON plugin_memory_acceptance_audit (item_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_memory_acceptance_audit_scope "
        "ON plugin_memory_acceptance_audit "
        "(tenant_id, channel, source_key, user_id, session_id, created_at DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_extraction_job_idempotency "
        "ON plugin_memory_extraction_job (idempotency_key)",
        "CREATE INDEX IF NOT EXISTS idx_memory_extraction_job_ready "
        "ON plugin_memory_extraction_job "
        "(status, next_run_at, locked_until, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_memory_extraction_job_scope "
        "ON plugin_memory_extraction_job "
        "(tenant_id, channel, source_key, user_id, session_id, created_at DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_entity_scope_name "
        "ON plugin_memory_entity "
        "(tenant_id, channel, source_key, user_id, entity_type, normalized_name)",
        "CREATE INDEX IF NOT EXISTS idx_memory_entity_scope "
        "ON plugin_memory_entity "
        "(tenant_id, channel, source_key, user_id, status, updated_at DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_fact_memory_item "
        "ON plugin_memory_fact (memory_item_id)",
        "CREATE INDEX IF NOT EXISTS idx_memory_fact_scope "
        "ON plugin_memory_fact "
        "(tenant_id, channel, source_key, user_id, status, updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_memory_fact_subject "
        "ON plugin_memory_fact (subject_entity_id, predicate, status)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_episode_memory_item "
        "ON plugin_memory_episode (memory_item_ids_json)",
        "CREATE INDEX IF NOT EXISTS idx_memory_episode_scope "
        "ON plugin_memory_episode "
        "(tenant_id, channel, source_key, user_id, session_id, status, updated_at DESC)",
    )


def _memory_legacy_data() -> None:
    _execute(
        """
        DO $$
        BEGIN
            IF to_regclass('plugin_memory_profile') IS NOT NULL THEN
                INSERT INTO plugin_memory_identity_profile (
                    tenant_id, channel, source_key, user_id, long_term_memory,
                    manual_notes, long_term_items_json, message_count,
                    imported_message_count, last_session_id, last_seen_at, updated_at
                )
                SELECT tenant_id, channel, source_key, user_id,
                    COALESCE(long_term_memory, ''), COALESCE(manual_notes, ''),
                    COALESCE(long_term_items_json, '[]'), COALESCE(message_count, 0),
                    0, COALESCE(last_session_id, ''), last_seen_at, updated_at
                FROM plugin_memory_profile legacy
                ON CONFLICT (tenant_id, channel, source_key, user_id) DO NOTHING;

                INSERT INTO plugin_memory_session_profile (
                    tenant_id, channel, source_key, session_id, user_id,
                    short_term_memory, manual_notes, short_term_items_json,
                    message_count, imported_message_count, last_seen_at, updated_at
                )
                SELECT tenant_id, channel, source_key,
                    COALESCE(NULLIF(last_session_id, ''), user_id), user_id,
                    COALESCE(short_term_memory, ''), '',
                    COALESCE(short_term_items_json, '[]'), COALESCE(message_count, 0),
                    0, last_seen_at, updated_at
                FROM plugin_memory_profile legacy
                ON CONFLICT (tenant_id, channel, source_key, session_id, user_id)
                DO NOTHING;
            END IF;
        END
        $$
        """,
    )


def _moderation_schema() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_moderation_config (
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            enabled BOOLEAN DEFAULT FALSE,
            webhook_url TEXT DEFAULT '',
            webhook_enabled BOOLEAN DEFAULT FALSE,
            reminder_mode VARCHAR(16) DEFAULT 'off',
            reminder_text TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (tenant_id, session_id)
        )
        """,
        "ALTER TABLE plugin_moderation_config "
        "ADD COLUMN IF NOT EXISTS reminder_text TEXT DEFAULT ''",
        "ALTER TABLE plugin_moderation_config "
        "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()",
        """
        CREATE TABLE IF NOT EXISTS plugin_moderation_keywords (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            keyword VARCHAR(256) NOT NULL,
            enabled BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        "DELETE FROM plugin_moderation_keywords WHERE BTRIM(COALESCE(keyword, '')) = ''",
        """
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY tenant_id, session_id, keyword ORDER BY id DESC
            ) AS rn
            FROM plugin_moderation_keywords
        )
        DELETE FROM plugin_moderation_keywords kw
        USING ranked
        WHERE kw.id = ranked.id AND ranked.rn > 1
        """,
        "CREATE INDEX IF NOT EXISTS ix_mod_kw_tenant_session "
        "ON plugin_moderation_keywords (tenant_id, session_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_mod_kw_tenant_session_keyword "
        "ON plugin_moderation_keywords (tenant_id, session_id, keyword)",
        """
        CREATE TABLE IF NOT EXISTS plugin_moderation_events (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            session_name VARCHAR(256) DEFAULT '',
            user_id VARCHAR(128) NOT NULL,
            sender_name VARCHAR(256) DEFAULT '',
            message_text TEXT NOT NULL,
            matched_keywords TEXT NOT NULL,
            action VARCHAR(32) DEFAULT 'flagged',
            webhook_status VARCHAR(16) DEFAULT '',
            trace_id VARCHAR(64) DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        "ALTER TABLE plugin_moderation_config ALTER COLUMN session_id TYPE VARCHAR(256)",
        "ALTER TABLE plugin_moderation_keywords ALTER COLUMN session_id TYPE VARCHAR(256)",
        "ALTER TABLE plugin_moderation_events ALTER COLUMN session_id TYPE VARCHAR(256)",
        "ALTER TABLE plugin_moderation_events "
        "ADD COLUMN IF NOT EXISTS session_name VARCHAR(256) DEFAULT ''",
        "ALTER TABLE plugin_moderation_events "
        "ADD COLUMN IF NOT EXISTS sender_name VARCHAR(256) DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS ix_mod_events_tenant_session "
        "ON plugin_moderation_events (tenant_id, session_id)",
        "CREATE INDEX IF NOT EXISTS ix_mod_events_tenant_created "
        "ON plugin_moderation_events (tenant_id, created_at DESC)",
    )


def _persona_schema() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_persona_jobs (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            session_name VARCHAR(256) NOT NULL DEFAULT '',
            target_user_id VARCHAR(128) NOT NULL,
            target_name VARCHAR(128) DEFAULT '',
            status VARCHAR(16) DEFAULT 'pending',
            msg_count INTEGER DEFAULT 0,
            days_limit INTEGER DEFAULT 90,
            max_messages INTEGER DEFAULT 2000,
            output_slug VARCHAR(128) DEFAULT '',
            mode VARCHAR(32) DEFAULT '',
            current_stage VARCHAR(32) DEFAULT 'queued',
            checkpoint_json TEXT DEFAULT '',
            result_text TEXT DEFAULT '',
            artifact_json TEXT DEFAULT '',
            error TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            completed_at TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_persona_profiles (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            channel VARCHAR(32) NOT NULL DEFAULT 'all',
            source_key VARCHAR(128) NOT NULL DEFAULT '*',
            source_label VARCHAR(128) DEFAULT '',
            profile_name VARCHAR(128) NOT NULL DEFAULT 'default',
            target_user_id VARCHAR(128) NOT NULL DEFAULT '',
            target_name VARCHAR(128) NOT NULL DEFAULT '',
            skill_slug VARCHAR(128) NOT NULL DEFAULT '',
            prompt_text TEXT NOT NULL DEFAULT '',
            artifact_json TEXT NOT NULL DEFAULT '',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            job_id BIGINT,
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (tenant_id, session_id, channel, source_key)
        )
        """,
        "ALTER TABLE plugin_persona_jobs ALTER COLUMN session_id TYPE VARCHAR(256)",
        "ALTER TABLE plugin_persona_profiles ALTER COLUMN session_id TYPE VARCHAR(256)",
        "ALTER TABLE plugin_persona_jobs ADD COLUMN IF NOT EXISTS "
        "session_name VARCHAR(256) NOT NULL DEFAULT ''",
        "ALTER TABLE plugin_persona_jobs ADD COLUMN IF NOT EXISTS "
        "output_slug VARCHAR(128) DEFAULT ''",
        "ALTER TABLE plugin_persona_jobs ADD COLUMN IF NOT EXISTS mode VARCHAR(32) DEFAULT ''",
        "ALTER TABLE plugin_persona_jobs ADD COLUMN IF NOT EXISTS "
        "current_stage VARCHAR(32) DEFAULT 'queued'",
        "ALTER TABLE plugin_persona_jobs ADD COLUMN IF NOT EXISTS checkpoint_json TEXT DEFAULT ''",
        "ALTER TABLE plugin_persona_jobs ADD COLUMN IF NOT EXISTS artifact_json TEXT DEFAULT ''",
        "ALTER TABLE plugin_persona_jobs ADD COLUMN IF NOT EXISTS "
        "updated_at TIMESTAMP DEFAULT NOW()",
        "ALTER TABLE plugin_persona_profiles ADD COLUMN IF NOT EXISTS "
        "target_user_id VARCHAR(128) NOT NULL DEFAULT ''",
        "ALTER TABLE plugin_persona_profiles ADD COLUMN IF NOT EXISTS "
        "target_name VARCHAR(128) NOT NULL DEFAULT ''",
        "ALTER TABLE plugin_persona_profiles ADD COLUMN IF NOT EXISTS "
        "skill_slug VARCHAR(128) NOT NULL DEFAULT ''",
        "ALTER TABLE plugin_persona_profiles ADD COLUMN IF NOT EXISTS "
        "artifact_json TEXT NOT NULL DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS ix_persona_jobs_tenant "
        "ON plugin_persona_jobs (tenant_id, session_id)",
        "CREATE INDEX IF NOT EXISTS ix_persona_jobs_target "
        "ON plugin_persona_jobs (tenant_id, session_id, target_user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_persona_profiles_scope "
        "ON plugin_persona_profiles "
        "(tenant_id, session_id, channel, source_key, enabled)",
        "CREATE INDEX IF NOT EXISTS ix_persona_profiles_target "
        "ON plugin_persona_profiles "
        "(tenant_id, session_id, target_user_id, updated_at DESC)",
    )


def _repeater_schema() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_repeater_config (
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            cooldown_seconds INTEGER NOT NULL DEFAULT 300,
            updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (tenant_id, session_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugin_repeater_event (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            content_hash VARCHAR(64) NOT NULL,
            content_text TEXT NOT NULL,
            trace_id VARCHAR(128) DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_repeater_event_lookup "
        "ON plugin_repeater_event "
        "(tenant_id, session_id, content_hash, created_at DESC)",
    )


def _tibo_reset_schema() -> None:
    _execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_tibo_reset_sync_state (
            id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            initialized BOOLEAN NOT NULL DEFAULT FALSE,
            last_poll_at TIMESTAMPTZ,
            last_success_at TIMESTAMPTZ,
            latest_tweet_id VARCHAR(64) NOT NULL DEFAULT '',
            fetched_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "INSERT INTO plugin_tibo_reset_sync_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING",
        """
        CREATE TABLE IF NOT EXISTS plugin_tibo_reset_feed (
            tweet_id VARCHAR(64) PRIMARY KEY,
            tweet_text TEXT NOT NULL,
            tweet_created_at TIMESTAMPTZ NOT NULL,
            source_url TEXT NOT NULL DEFAULT '',
            confidence DOUBLE PRECISION,
            evidence TEXT NOT NULL DEFAULT '',
            stated_reason TEXT NOT NULL DEFAULT '',
            reset_type VARCHAR(64) NOT NULL DEFAULT '',
            beneficiaries VARCHAR(64) NOT NULL DEFAULT '',
            content_valid BOOLEAN NOT NULL DEFAULT FALSE,
            validation_reason VARCHAR(64) NOT NULL DEFAULT '',
            after_baseline BOOLEAN NOT NULL DEFAULT FALSE,
            notify_eligible BOOLEAN NOT NULL DEFAULT TRUE,
            discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "ALTER TABLE plugin_tibo_reset_feed ADD COLUMN IF NOT EXISTS "
        "content_valid BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE plugin_tibo_reset_feed ADD COLUMN IF NOT EXISTS "
        "validation_reason VARCHAR(64) NOT NULL DEFAULT ''",
        "ALTER TABLE plugin_tibo_reset_feed ADD COLUMN IF NOT EXISTS "
        "after_baseline BOOLEAN NOT NULL DEFAULT FALSE",
        "CREATE INDEX IF NOT EXISTS idx_tibo_reset_feed_delivery "
        "ON plugin_tibo_reset_feed "
        "(notify_eligible, discovered_at, tweet_created_at)",
        "CREATE INDEX IF NOT EXISTS idx_tibo_reset_feed_stats "
        "ON plugin_tibo_reset_feed (content_valid, tweet_created_at DESC)",
        """
        CREATE TABLE IF NOT EXISTS plugin_tibo_reset_delivery (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(128) NOT NULL,
            session_id VARCHAR(256) NOT NULL,
            session_name VARCHAR(256) NOT NULL DEFAULT '',
            tweet_id VARCHAR(64) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'running',
            attempt_count INTEGER NOT NULL DEFAULT 1,
            reply_queue_id BIGINT,
            command_id VARCHAR(128) NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            started_at TIMESTAMPTZ,
            next_attempt_at TIMESTAMPTZ,
            queued_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, session_id, tweet_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_tibo_reset_delivery_status "
        "ON plugin_tibo_reset_delivery (status, next_attempt_at, updated_at)",
    )
