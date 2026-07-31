"""Add durable memory provenance and expiry metadata.

Revision ID: 0046_memory_event_provenance_expiry
Revises: 0045_wxbot_outbound_files
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0046_memory_event_provenance_expiry"
down_revision = "0045_wxbot_outbound_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plugin_memory_event",
        sa.Column(
            "source_member_id",
            sa.String(length=128),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "plugin_memory_event",
        sa.Column(
            "source_message_id",
            sa.String(length=256),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "plugin_memory_event",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "plugin_memory_item",
        sa.Column(
            "source_evidence_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # Recover member provenance from the historical group transcript prefix.
    # Non-group rows were already scoped to the actual member.
    op.execute(
        """
        UPDATE plugin_memory_event
        SET source_member_id = CASE
                WHEN user_id = '__group__'
                    THEN COALESCE(
                        substring(user_text FROM '^([A-Za-z0-9_@.\\-]+):\\s+'),
                        ''
                    )
                ELSE user_id
            END,
            source_message_id = COALESCE(
                NULLIF(event_key, ''),
                NULLIF(trace_id, ''),
                'legacy-memory-event:' || id::text
            ),
            expires_at = COALESCE(
                expires_at,
                created_at + INTERVAL '180 days'
            )
        """
    )

    # Backfill direct and known multi-event evidence shapes used by automatic,
    # graph and group-window memory items.
    op.execute(
        """
        WITH item_json AS (
            SELECT
                item.id AS item_id,
                COALESCE(NULLIF(item.value_json, '')::jsonb, '{}'::jsonb) AS value
            FROM plugin_memory_item AS item
        ),
        evidence_ids AS (
            SELECT id AS item_id, source_event_id AS event_id
            FROM plugin_memory_item
            WHERE source_event_id IS NOT NULL

            UNION

            SELECT item_json.item_id, raw.value::bigint
            FROM item_json
            CROSS JOIN LATERAL jsonb_array_elements_text(
                CASE
                    WHEN jsonb_typeof(item_json.value -> 'source_event_ids') = 'array'
                        THEN item_json.value -> 'source_event_ids'
                    ELSE '[]'::jsonb
                END
            ) AS raw(value)
            WHERE raw.value ~ '^[0-9]+$'

            UNION

            SELECT item_json.item_id, raw.value::bigint
            FROM item_json
            CROSS JOIN LATERAL jsonb_array_elements_text(
                CASE
                    WHEN jsonb_typeof(item_json.value #> '{relation,evidence_event_ids}') = 'array'
                        THEN item_json.value #> '{relation,evidence_event_ids}'
                    ELSE '[]'::jsonb
                END
            ) AS raw(value)
            WHERE raw.value ~ '^[0-9]+$'

            UNION

            SELECT
                item_json.item_id,
                (raw.value ->> 'source_event_id')::bigint
            FROM item_json
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(item_json.value -> 'evidence') = 'array'
                        THEN item_json.value -> 'evidence'
                    ELSE '[]'::jsonb
                END
            ) AS raw(value)
            WHERE COALESCE(raw.value ->> 'source_event_id', '') ~ '^[0-9]+$'
        ),
        evidence AS (
            SELECT
                ids.item_id,
                jsonb_agg(
                    DISTINCT jsonb_build_object(
                        'source_event_id', event.id,
                        'source_member_id', event.source_member_id,
                        'source_message_id', event.source_message_id
                    )
                ) AS source_evidence
            FROM evidence_ids AS ids
            JOIN plugin_memory_event AS event ON event.id = ids.event_id
            GROUP BY ids.item_id
        )
        UPDATE plugin_memory_item AS item
        SET source_evidence_json = evidence.source_evidence
        FROM evidence
        WHERE item.id = evidence.item_id
        """
    )

    op.create_index(
        "ix_memory_event_member_evidence",
        "plugin_memory_event",
        ["tenant_id", "channel", "source_member_id", "session_id", "id"],
    )
    op.create_index(
        "ix_memory_event_source_message",
        "plugin_memory_event",
        ["tenant_id", "channel", "source_key", "session_id", "source_message_id"],
    )
    op.create_index(
        "ix_memory_event_expiry",
        "plugin_memory_event",
        ["expires_at", "id"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_index(
        "ix_memory_item_expiry_physical",
        "plugin_memory_item",
        ["expires_at", "id"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_index(
        "ix_memory_item_source_evidence",
        "plugin_memory_item",
        ["source_evidence_json"],
        postgresql_using="gin",
    )
    # The index predates this migration, but the normal interaction writer now
    # relies on it as its projection idempotency gate.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_event_key "
        "ON plugin_memory_event (event_key) WHERE event_key IS NOT NULL"
    )
    op.execute(
        "UPDATE app_schema_contract SET compatibility_level = 9 "
        "WHERE contract_name = 'agent-console-runtime'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE app_schema_contract SET compatibility_level = 8 "
        "WHERE contract_name = 'agent-console-runtime'"
    )
    op.drop_index("ix_memory_item_source_evidence", table_name="plugin_memory_item")
    op.drop_index("ix_memory_item_expiry_physical", table_name="plugin_memory_item")
    op.drop_index("ix_memory_event_expiry", table_name="plugin_memory_event")
    op.drop_index("ix_memory_event_source_message", table_name="plugin_memory_event")
    op.drop_index("ix_memory_event_member_evidence", table_name="plugin_memory_event")
    op.drop_column("plugin_memory_item", "source_evidence_json")
    op.drop_column("plugin_memory_event", "expires_at")
    op.drop_column("plugin_memory_event", "source_message_id")
    op.drop_column("plugin_memory_event", "source_member_id")
