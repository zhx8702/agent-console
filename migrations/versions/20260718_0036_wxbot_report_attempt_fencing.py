"""Fence wxbot report and self-review worker attempts.

Revision ID: 0036_wxbot_report_attempt_fencing
Revises: 0035_plugin_lifecycle_global_index
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_wxbot_report_attempt_fencing"
down_revision = "0035_plugin_lifecycle_global_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fencing cannot retroactively constrain a worker that started with the
    # pre-0036 SQL.  Lock both queues in one fixed order, then refuse the
    # migration while external work or delivery is active.  Deployments must
    # stop old workers and drain these states before retrying the migration.
    op.execute(
        "LOCK TABLE plugin_wxbot_report_jobs IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        "LOCK TABLE plugin_wxbot_self_review_jobs IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM plugin_wxbot_report_jobs
                WHERE status = 'running' OR delivery_status = 'sending'
            ) OR EXISTS (
                SELECT 1 FROM plugin_wxbot_self_review_jobs
                WHERE status = 'running'
            ) THEN
                RAISE EXCEPTION
                    'cannot upgrade 0036: drain active wxbot report jobs first';
            END IF;
        END
        $$
        """
    )
    op.add_column(
        "plugin_wxbot_report_jobs",
        sa.Column(
            "run_attempt",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "plugin_wxbot_report_jobs",
        sa.Column(
            "delivery_attempt",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "plugin_wxbot_self_review_jobs",
        sa.Column(
            "run_attempt",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_index(
        "idx_wxbot_report_jobs_running_lease",
        "plugin_wxbot_report_jobs",
        ["updated_at"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "idx_wxbot_report_jobs_sending_lease",
        "plugin_wxbot_report_jobs",
        ["updated_at"],
        unique=False,
        postgresql_where=sa.text("delivery_status = 'sending'"),
    )
    op.create_index(
        "idx_wxbot_self_review_jobs_running_lease",
        "plugin_wxbot_self_review_jobs",
        ["updated_at"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.execute(
        "UPDATE app_schema_contract SET compatibility_level = 4 "
        "WHERE contract_name = 'agent-console-runtime'"
    )


def downgrade() -> None:
    # Keep the guard and destructive DDL in one write-exclusive critical
    # section.  The order matches upgrade so concurrent migration attempts
    # cannot deadlock by acquiring the queue tables in opposite directions.
    op.execute(
        "LOCK TABLE plugin_wxbot_report_jobs IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        "LOCK TABLE plugin_wxbot_self_review_jobs IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM plugin_wxbot_report_jobs
                WHERE status = 'running' OR delivery_status = 'sending'
            ) OR EXISTS (
                SELECT 1 FROM plugin_wxbot_self_review_jobs
                WHERE status = 'running'
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 0036: drain active wxbot report jobs first';
            END IF;
        END
        $$
        """
    )
    op.execute(
        "UPDATE app_schema_contract SET compatibility_level = 3 "
        "WHERE contract_name = 'agent-console-runtime'"
    )
    op.drop_index(
        "idx_wxbot_self_review_jobs_running_lease",
        table_name="plugin_wxbot_self_review_jobs",
    )
    op.drop_index(
        "idx_wxbot_report_jobs_sending_lease",
        table_name="plugin_wxbot_report_jobs",
    )
    op.drop_index(
        "idx_wxbot_report_jobs_running_lease",
        table_name="plugin_wxbot_report_jobs",
    )
    op.drop_column("plugin_wxbot_self_review_jobs", "run_attempt")
    op.drop_column("plugin_wxbot_report_jobs", "delivery_attempt")
    op.drop_column("plugin_wxbot_report_jobs", "run_attempt")
