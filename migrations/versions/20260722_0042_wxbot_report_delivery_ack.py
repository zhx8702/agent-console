"""Persist wxbot report SDK delivery acknowledgements.

Revision ID: 0042_wxbot_report_delivery_ack
Revises: 0041_persona_job_queue
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_wxbot_report_delivery_ack"
down_revision = "0041_persona_job_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Delivery state and the SDK queue row id must move atomically.  The lock
    # also prevents an old worker from creating another unidentifiable
    # sending/sent transition while legacy rows are quarantined.
    op.execute("LOCK TABLE plugin_wxbot_report_jobs IN ACCESS EXCLUSIVE MODE")
    op.add_column(
        "plugin_wxbot_report_jobs",
        sa.Column("sdk_outbound_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "plugin_wxbot_report_jobs",
        sa.Column(
            "delivery_queued_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "plugin_wxbot_report_jobs",
        sa.Column(
            "delivery_checked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.execute(
        "UPDATE plugin_wxbot_report_jobs "
        "SET delivery_status = 'indeterminate', "
        "delivery_error = 'legacy delivery has no SDK outbound row id; outcome cannot be reconciled', "
        "delivered_at = NULL, "
        "updated_at = NOW() "
        "WHERE delivery_status IN ('sent', 'sending') "
        "AND sdk_outbound_id IS NULL"
    )
    op.create_index(
        "idx_wxbot_report_jobs_queued_delivery",
        "plugin_wxbot_report_jobs",
        ["tenant_id", "delivery_checked_at", "delivery_queued_at", "id"],
        unique=False,
        postgresql_where=sa.text("delivery_status = 'queued'"),
    )
    op.execute(
        "UPDATE app_schema_contract SET compatibility_level = 6 "
        "WHERE contract_name = 'agent-console-runtime'"
    )


def downgrade() -> None:
    op.execute("LOCK TABLE plugin_wxbot_report_jobs IN ACCESS EXCLUSIVE MODE")
    # A queued SDK row may already have been delivered.  Older code cannot
    # reconcile it, so quarantine it instead of turning it into a retry.
    op.execute(
        "UPDATE plugin_wxbot_report_jobs "
        "SET delivery_status = 'indeterminate', "
        "delivery_error = 'SDK delivery acknowledgement unavailable after schema downgrade', "
        "updated_at = NOW() "
        "WHERE delivery_status = 'queued'"
    )
    op.execute(
        "UPDATE app_schema_contract SET compatibility_level = 5 "
        "WHERE contract_name = 'agent-console-runtime'"
    )
    op.drop_index(
        "idx_wxbot_report_jobs_queued_delivery",
        table_name="plugin_wxbot_report_jobs",
    )
    op.drop_column("plugin_wxbot_report_jobs", "delivery_checked_at")
    op.drop_column("plugin_wxbot_report_jobs", "delivery_queued_at")
    op.drop_column("plugin_wxbot_report_jobs", "sdk_outbound_id")
