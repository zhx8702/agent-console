"""Add optimistic concurrency to group-scoped plugin switches.

Revision ID: 0030_plugin_scope_concurrency
Revises: 0029_wxbot_admin_mutation_state
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_plugin_scope_concurrency"
down_revision = "0029_wxbot_admin_mutation_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plugin_scope_state",
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_check_constraint(
        "ck_plugin_scope_state_version_positive",
        "plugin_scope_state",
        "version > 0",
    )
    op.execute(
        "UPDATE app_schema_contract SET compatibility_level = 2 "
        "WHERE contract_name = 'agent-console-runtime'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE app_schema_contract SET compatibility_level = 1 "
        "WHERE contract_name = 'agent-console-runtime'"
    )
    op.drop_constraint(
        "ck_plugin_scope_state_version_positive",
        "plugin_scope_state",
        type_="check",
    )
    op.drop_column("plugin_scope_state", "version")
