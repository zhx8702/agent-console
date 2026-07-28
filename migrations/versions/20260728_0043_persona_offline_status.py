"""Allow persona offline exports to wait for generated artifact import.

Revision ID: 0043_persona_offline_status
Revises: 0042_wxbot_report_delivery_ack
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op

revision = "0043_persona_offline_status"
down_revision = "0042_wxbot_report_delivery_ack"
branch_labels = None
depends_on = None

_TABLE = "plugin_persona_jobs"
_CONSTRAINT = "ck_persona_jobs_status"
_ONLINE_STATUSES = (
    "'pending', 'running', 'retry_wait', 'completed', 'failed', 'cancelled'"
)


def upgrade() -> None:
    op.execute(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE")
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        f"status IN ({_ONLINE_STATUSES}, 'awaiting_import')",
    )


def downgrade() -> None:
    op.execute(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM {_TABLE} WHERE status = 'awaiting_import'
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 0043: import or cancel offline persona jobs first';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        f"status IN ({_ONLINE_STATUSES})",
    )
