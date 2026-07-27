"""expand session and turn tenant scope

Revision ID: 0011_session_tenant_scope_expand
Revises: 0010_memory_extraction_job_result
Create Date: 2026-07-16

This is the rolling-deploy-compatible expand phase:

* existing sessions keep their legacy ``session_id`` primary key;
* turns gain a nullable ``tenant_id`` so an old process can still insert;
* existing turns are backfilled and new code dual-writes tenant_id;
* a composite unique key/FK lets new code enforce tenant ownership;
* a database trigger makes session tenant ownership immutable.

A later contract migration can make ``turns.tenant_id`` non-null and promote
``(tenant_id, session_id)`` to the sessions primary key after every old process
has been drained and the final NULL backfill is complete.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_session_tenant_scope_expand"
down_revision = "0010_memory_extraction_job_result"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "turns",
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
    )
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

    op.create_unique_constraint(
        "uq_sessions_tenant_session",
        "sessions",
        ["tenant_id", "session_id"],
    )
    op.create_foreign_key(
        "fk_turns_tenant_session",
        "turns",
        "sessions",
        ["tenant_id", "session_id"],
        ["tenant_id", "session_id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_turns_tenant_session_created",
        "turns",
        ["tenant_id", "session_id", "created_at"],
    )

    # Keep tenant ownership immutable even while an old application version
    # still queries by the legacy global session_id primary key.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION cs_reject_session_tenant_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN
                RAISE EXCEPTION 'session tenant_id is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_sessions_tenant_immutable
        BEFORE UPDATE OF tenant_id ON sessions
        FOR EACH ROW
        EXECUTE FUNCTION cs_reject_session_tenant_change()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_sessions_tenant_immutable ON sessions"
    )
    op.execute("DROP FUNCTION IF EXISTS cs_reject_session_tenant_change()")
    op.drop_index("ix_turns_tenant_session_created", table_name="turns")
    op.drop_constraint(
        "fk_turns_tenant_session",
        "turns",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_sessions_tenant_session",
        "sessions",
        type_="unique",
    )
    op.drop_column("turns", "tenant_id")
