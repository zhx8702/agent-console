"""Make persona extraction durable, resumable, and chunk-aware.

Revision ID: 0041_persona_job_queue
Revises: 0039_channel_connection_activity
Create Date: 2026-07-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041_persona_job_queue"
down_revision = "0039_channel_connection_activity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Old replicas claim persona jobs without a fencing token.  Keep the
    # compatibility change and the queue DDL in one exclusive critical section
    # so an old worker cannot start after the migration guard has run.
    op.execute("LOCK TABLE plugin_persona_jobs IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM plugin_persona_jobs WHERE status = 'running'
            ) THEN
                RAISE EXCEPTION
                    'cannot upgrade 0041: drain active persona jobs first';
            END IF;
        END
        $$
        """
    )
    op.execute(
        "UPDATE plugin_persona_jobs SET status = 'pending' WHERE status IS NULL"
    )
    op.alter_column(
        "plugin_persona_jobs",
        "status",
        existing_type=sa.String(length=16),
        nullable=False,
    )

    op.add_column(
        "plugin_persona_jobs",
        sa.Column(
            "run_attempt",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "plugin_persona_jobs",
        sa.Column(
            "retry_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "plugin_persona_jobs",
        sa.Column(
            "claim_owner",
            sa.String(length=128),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "plugin_persona_jobs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "plugin_persona_jobs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "plugin_persona_jobs",
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.add_column(
        "plugin_persona_jobs",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "plugin_persona_jobs",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "plugin_persona_jobs",
        sa.Column(
            "request_id",
            sa.String(length=128),
            nullable=True,
        ),
    )
    op.add_column(
        "plugin_persona_jobs",
        sa.Column(
            "request_hash",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "plugin_persona_jobs",
        sa.Column(
            "input_messages_json",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "plugin_persona_jobs",
        sa.Column(
            "total_chunks",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "plugin_persona_jobs",
        sa.Column(
            "completed_chunks",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "ck_persona_jobs_attempts_nonnegative",
        "plugin_persona_jobs",
        "run_attempt >= 0 AND retry_count >= 0",
    )
    op.create_check_constraint(
        "ck_persona_jobs_chunk_progress",
        "plugin_persona_jobs",
        "total_chunks >= 0 AND completed_chunks >= 0 "
        "AND completed_chunks <= total_chunks",
    )
    op.create_check_constraint(
        "ck_persona_jobs_status",
        "plugin_persona_jobs",
        "status IN ('pending', 'running', 'retry_wait', 'completed', "
        "'failed', 'cancelled')",
    )
    op.create_check_constraint(
        "ck_persona_jobs_lease_state",
        "plugin_persona_jobs",
        "(status = 'running' AND run_attempt > 0 AND claim_owner <> '' "
        "AND lease_expires_at IS NOT NULL) OR "
        "(status <> 'running' AND claim_owner = '' "
        "AND lease_expires_at IS NULL)",
    )
    op.create_unique_constraint(
        "uq_persona_jobs_tenant_request",
        "plugin_persona_jobs",
        ["tenant_id", "request_id"],
    )
    op.create_index(
        "idx_persona_jobs_ready",
        "plugin_persona_jobs",
        ["available_at", "created_at", "id"],
        unique=False,
        postgresql_where=sa.text(
            "(status = 'pending' OR status = 'retry_wait') "
            "AND cancel_requested_at IS NULL"
        ),
    )
    op.create_index(
        "idx_persona_jobs_running_lease",
        "plugin_persona_jobs",
        ["lease_expires_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "plugin_persona_job_chunks",
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column(
            "result_json",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "error",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "chunk_index >= 0 AND message_count > 0 AND estimated_tokens > 0",
            name="ck_persona_job_chunks_bounds",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_persona_job_chunks_status",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["plugin_persona_jobs.id"],
            name="fk_persona_job_chunks_job",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "job_id",
            "chunk_index",
            name="pk_persona_job_chunks",
        ),
    )
    op.create_index(
        "idx_persona_job_chunks_status",
        "plugin_persona_job_chunks",
        ["job_id", "status", "chunk_index"],
        unique=False,
    )
    op.execute(
        "UPDATE app_schema_contract SET compatibility_level = 5 "
        "WHERE contract_name = 'agent-console-runtime'"
    )


def downgrade() -> None:
    op.execute("LOCK TABLE plugin_persona_jobs IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM plugin_persona_jobs
                WHERE status IN ('pending', 'running', 'retry_wait')
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 0041: finish or cancel persona jobs first';
            END IF;
        END
        $$
        """
    )
    op.execute(
        "UPDATE app_schema_contract SET compatibility_level = 4 "
        "WHERE contract_name = 'agent-console-runtime'"
    )
    op.drop_index(
        "idx_persona_job_chunks_status",
        table_name="plugin_persona_job_chunks",
    )
    op.drop_table("plugin_persona_job_chunks")
    op.drop_index("idx_persona_jobs_running_lease", table_name="plugin_persona_jobs")
    op.drop_index("idx_persona_jobs_ready", table_name="plugin_persona_jobs")
    op.drop_constraint(
        "uq_persona_jobs_tenant_request",
        "plugin_persona_jobs",
        type_="unique",
    )
    op.drop_constraint(
        "ck_persona_jobs_lease_state",
        "plugin_persona_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_persona_jobs_status",
        "plugin_persona_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_persona_jobs_chunk_progress",
        "plugin_persona_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_persona_jobs_attempts_nonnegative",
        "plugin_persona_jobs",
        type_="check",
    )
    op.alter_column(
        "plugin_persona_jobs",
        "status",
        existing_type=sa.String(length=16),
        nullable=True,
    )
    for column in (
        "completed_chunks",
        "total_chunks",
        "input_messages_json",
        "request_hash",
        "request_id",
        "started_at",
        "cancel_requested_at",
        "available_at",
        "heartbeat_at",
        "lease_expires_at",
        "claim_owner",
        "retry_count",
        "run_attempt",
    ):
        op.drop_column("plugin_persona_jobs", column)
