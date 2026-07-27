"""add memory graph projection tables

Revision ID: 0006_memory_graph
Revises: 0005_memory_session_state
Create Date: 2026-05-11

"""
from __future__ import annotations

from alembic import op

revision = "0006_memory_graph"
down_revision = "0005_memory_session_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS plugin_memory_entity (
            id                      BIGSERIAL PRIMARY KEY,
            tenant_id               VARCHAR(64) NOT NULL,
            channel                 VARCHAR(32) NOT NULL,
            source_key              VARCHAR(128) NOT NULL DEFAULT '*',
            user_id                 VARCHAR(128) NOT NULL,
            entity_type             VARCHAR(64) NOT NULL DEFAULT 'thing',
            name                    TEXT NOT NULL DEFAULT '',
            normalized_name         VARCHAR(128) NOT NULL,
            aliases_json            TEXT DEFAULT '[]',
            confidence              DOUBLE PRECISION DEFAULT 0.0,
            status                  VARCHAR(32) NOT NULL DEFAULT 'active',
            created_at              TIMESTAMP DEFAULT NOW(),
            updated_at              TIMESTAMP DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS plugin_memory_fact (
            id                      BIGSERIAL PRIMARY KEY,
            tenant_id               VARCHAR(64) NOT NULL,
            channel                 VARCHAR(32) NOT NULL,
            source_key              VARCHAR(128) NOT NULL DEFAULT '*',
            user_id                 VARCHAR(128) NOT NULL,
            subject_entity_id       BIGINT NOT NULL,
            predicate               VARCHAR(128) NOT NULL,
            object_entity_id        BIGINT NULL,
            object_value            TEXT DEFAULT '',
            memory_item_id          BIGINT NOT NULL,
            source_event_id         BIGINT NULL,
            confidence              DOUBLE PRECISION DEFAULT 0.0,
            status                  VARCHAR(32) NOT NULL DEFAULT 'active',
            valid_at                TIMESTAMP DEFAULT NOW(),
            invalid_at              TIMESTAMP NULL,
            created_at              TIMESTAMP DEFAULT NOW(),
            updated_at              TIMESTAMP DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS plugin_memory_episode (
            id                      BIGSERIAL PRIMARY KEY,
            tenant_id               VARCHAR(64) NOT NULL,
            channel                 VARCHAR(32) NOT NULL,
            source_key              VARCHAR(128) NOT NULL DEFAULT '*',
            user_id                 VARCHAR(128) NOT NULL,
            session_id              VARCHAR(256) DEFAULT '',
            title                   TEXT DEFAULT '',
            summary                 TEXT DEFAULT '',
            event_ids_json          TEXT DEFAULT '[]',
            memory_item_ids_json    TEXT DEFAULT '[]',
            importance              INTEGER NOT NULL DEFAULT 0,
            status                  VARCHAR(32) NOT NULL DEFAULT 'active',
            created_at              TIMESTAMP DEFAULT NOW(),
            updated_at              TIMESTAMP DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_entity_scope_name
        ON plugin_memory_entity (
            tenant_id, channel, source_key, user_id, entity_type, normalized_name
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_entity_scope
        ON plugin_memory_entity (
            tenant_id, channel, source_key, user_id, status, updated_at DESC
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_fact_memory_item
        ON plugin_memory_fact (memory_item_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_fact_scope
        ON plugin_memory_fact (
            tenant_id, channel, source_key, user_id, status, updated_at DESC
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_fact_subject
        ON plugin_memory_fact (subject_entity_id, predicate, status)
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_episode_memory_item
        ON plugin_memory_episode (memory_item_ids_json)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_episode_scope
        ON plugin_memory_episode (
            tenant_id, channel, source_key, user_id, session_id, status, updated_at DESC
        )
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_memory_episode_scope")
    op.execute("DROP INDEX IF EXISTS ux_memory_episode_memory_item")
    op.execute("DROP INDEX IF EXISTS idx_memory_fact_subject")
    op.execute("DROP INDEX IF EXISTS idx_memory_fact_scope")
    op.execute("DROP INDEX IF EXISTS ux_memory_fact_memory_item")
    op.execute("DROP INDEX IF EXISTS idx_memory_entity_scope")
    op.execute("DROP INDEX IF EXISTS ux_memory_entity_scope_name")
    op.execute("DROP TABLE IF EXISTS plugin_memory_episode")
    op.execute("DROP TABLE IF EXISTS plugin_memory_fact")
    op.execute("DROP TABLE IF EXISTS plugin_memory_entity")
