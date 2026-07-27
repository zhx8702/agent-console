"""Persist the producer owner for durable message effect intents.

Revision ID: 0034_message_effect_producer_owner
Revises: 0033_wxbot_event_connection_scope
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034_message_effect_producer_owner"
down_revision = "0033_wxbot_event_connection_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "message_effect_intent",
        sa.Column("producer_owner", sa.String(length=128), nullable=True),
    )
    # The producer of an old cross-owner intent cannot be reconstructed from
    # its handler owner. Refuse the upgrade while executable work exists so an
    # operator must drain it with the old relay instead of silently granting
    # it the handler's execution authority.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM message_effect_intent
                WHERE status IN ('prepared', 'running')
                   OR (status = 'failed' AND available_at IS NOT NULL)
            ) THEN
                RAISE EXCEPTION
                    'cannot upgrade 0034: drain executable effect intents before '
                    'backfilling producer provenance';
            END IF;
        END
        $$
        """
    )
    # Before producer provenance was persisted, the handler owner was the only
    # durable owner identity. Once all executable work is drained, preserving
    # it for terminal history is safe and keeps audit records self-contained.
    op.execute(
        "UPDATE message_effect_intent "
        "SET producer_owner = owner "
        "WHERE producer_owner IS NULL"
    )
    op.alter_column(
        "message_effect_intent",
        "producer_owner",
        existing_type=sa.String(length=128),
        nullable=False,
    )
    # New writers must persist producer provenance. Bump the contract only
    # after the column and backfill are complete so incompatible replicas fail
    # closed instead of writing incomplete intents.
    op.execute(
        "UPDATE app_schema_contract SET compatibility_level = 3 "
        "WHERE contract_name = 'agent-console-runtime'"
    )


def downgrade() -> None:
    # Dropping producer provenance would let an older relay execute pending
    # work after checking only the handler owner. Require operators to drain
    # intents whose producer differs before intentionally downgrading.
    # Acquire the write-blocking lock before inspecting the queue.  Without
    # it, a concurrent producer could insert a cross-owner intent after the
    # guard succeeds but before DROP COLUMN obtains its own table lock.
    op.execute("LOCK TABLE message_effect_intent IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM message_effect_intent
                WHERE producer_owner <> owner
                  AND (
                      status IN ('prepared', 'running')
                      OR (status = 'failed' AND available_at IS NOT NULL)
                  )
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 0034: pending effect intents require producer provenance';
            END IF;
        END
        $$
        """
    )
    op.execute(
        "UPDATE app_schema_contract SET compatibility_level = 2 "
        "WHERE contract_name = 'agent-console-runtime'"
    )
    op.drop_column("message_effect_intent", "producer_owner")
