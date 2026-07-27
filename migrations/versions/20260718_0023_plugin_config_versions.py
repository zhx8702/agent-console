"""Add optimistic-concurrency versions to mutable plugin configuration.

Revision ID: 0023_plugin_config_versions
Revises: 0022_social_scope_controls
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_plugin_config_versions"
down_revision = "0022_social_scope_controls"
branch_labels = None
depends_on = None

_CONFIG_TABLES = (
    "plugin_command_center_config",
    "plugin_moderation_config",
    "plugin_group_activity_config",
)


def upgrade() -> None:
    for table_name in _CONFIG_TABLES:
        op.add_column(
            table_name,
            sa.Column(
                "version",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )
        op.create_check_constraint(
            f"ck_{table_name}_version_positive",
            table_name,
            "version > 0",
        )

    # A legacy installation can contain moderation keywords without a config
    # row. Materialize those resources so every existing keyword collection
    # starts with a durable version before conditional writes are enabled.
    op.execute(
        """
        INSERT INTO plugin_moderation_config (tenant_id, session_id, version)
        SELECT DISTINCT tenant_id, session_id, 1
        FROM plugin_moderation_keywords
        ON CONFLICT (tenant_id, session_id) DO NOTHING
        """
    )


def downgrade() -> None:
    for table_name in reversed(_CONFIG_TABLES):
        op.drop_constraint(
            f"ck_{table_name}_version_positive",
            table_name,
            type_="check",
        )
        op.drop_column(table_name, "version")
