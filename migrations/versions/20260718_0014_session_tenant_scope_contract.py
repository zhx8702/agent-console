"""contract tenant-scoped sessions and add fencing watermark

Revision ID: 0014_session_tenant_scope_contract
Revises: 0013_wxbot_participation_queue
Create Date: 2026-07-18

This contract step must run only after every expand-stage application process
has been drained. It finishes the online migration by making the tenant scope
part of the physical session identity and rejecting tenant-less turns.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_session_tenant_scope_contract"
down_revision = "0013_wxbot_participation_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Close the rolling-deploy window and prove the NOT NULL operation is safe.
    op.execute(
        sa.text(
            """
            UPDATE turns AS turn_row
            SET tenant_id = session_row.tenant_id
            FROM sessions AS session_row
            WHERE turn_row.session_id = session_row.session_id
              AND turn_row.tenant_id IS NULL
            """
        )
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM turns WHERE tenant_id IS NULL) THEN
                RAISE EXCEPTION
                    'cannot contract session tenant scope: tenant-less turns remain';
            END IF;
        END
        $$
        """
    )
    op.alter_column(
        "turns",
        "tenant_id",
        existing_type=sa.String(length=64),
        nullable=False,
    )

    # Rebuild both FKs around the physical composite identity. The expand-stage
    # unique constraint becomes redundant once the same columns are the PK.
    op.drop_constraint(
        "fk_turns_tenant_session",
        "turns",
        type_="foreignkey",
    )
    op.drop_constraint(
        "turns_session_id_fkey",
        "turns",
        type_="foreignkey",
    )
    op.drop_constraint("sessions_pkey", "sessions", type_="primary")
    op.create_primary_key(
        "pk_sessions_tenant_session",
        "sessions",
        ["tenant_id", "session_id"],
    )
    op.drop_constraint(
        "uq_sessions_tenant_session",
        "sessions",
        type_="unique",
    )
    op.create_foreign_key(
        "fk_turns_tenant_session",
        "turns",
        "sessions",
        ["tenant_id", "session_id"],
        ["tenant_id", "session_id"],
        ondelete="CASCADE",
    )

    op.add_column(
        "sessions",
        sa.Column(
            "fence_token",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    # A legacy global session key cannot represent duplicate ids. Refuse a
    # destructive downgrade instead of silently merging tenant data.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT session_id
                FROM sessions
                GROUP BY session_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'cannot restore global session primary key: duplicate ids exist';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint(
        "fk_turns_tenant_session",
        "turns",
        type_="foreignkey",
    )
    op.drop_column("sessions", "fence_token")
    op.drop_constraint(
        "pk_sessions_tenant_session",
        "sessions",
        type_="primary",
    )
    op.create_primary_key(
        "sessions_pkey",
        "sessions",
        ["session_id"],
    )
    op.create_unique_constraint(
        "uq_sessions_tenant_session",
        "sessions",
        ["tenant_id", "session_id"],
    )
    op.create_foreign_key(
        "turns_session_id_fkey",
        "turns",
        "sessions",
        ["session_id"],
        ["session_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_turns_tenant_session",
        "turns",
        "sessions",
        ["tenant_id", "session_id"],
        ["tenant_id", "session_id"],
        ondelete="CASCADE",
    )
    op.alter_column(
        "turns",
        "tenant_id",
        existing_type=sa.String(length=64),
        nullable=True,
    )
