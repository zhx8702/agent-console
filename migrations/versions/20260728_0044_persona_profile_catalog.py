"""Allow multiple saved persona skills with one active skill per scope.

Revision ID: 0044_persona_profile_catalog
Revises: 0043_persona_offline_status
Create Date: 2026-07-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_persona_profile_catalog"
down_revision = "0043_persona_offline_status"
branch_labels = None
depends_on = None

_TABLE = "plugin_persona_profiles"
_LEGACY_SCOPE_CONSTRAINT = (
    "plugin_persona_profiles_tenant_id_session_id_channel_source_key"
)
_ACTIVE_SCOPE_INDEX = "ux_persona_profiles_active_scope"
_SCOPE_SKILL_INDEX = "ux_persona_profiles_scope_skill"


def upgrade() -> None:
    op.execute(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE")
    op.drop_constraint(
        _LEGACY_SCOPE_CONSTRAINT,
        _TABLE,
        type_="unique",
    )
    op.create_index(
        "ux_persona_profiles_active_scope",
        _TABLE,
        ["tenant_id", "session_id", "channel", "source_key"],
        unique=True,
        postgresql_where=sa.text("enabled"),
    )
    op.create_index(
        "ux_persona_profiles_scope_skill",
        _TABLE,
        ["tenant_id", "session_id", "channel", "source_key", "skill_slug"],
        unique=True,
        postgresql_where=sa.text("skill_slug <> ''"),
    )
    op.execute(
        "UPDATE app_schema_contract SET compatibility_level = 7 "
        "WHERE contract_name = 'agent-console-runtime'"
    )


def downgrade() -> None:
    op.execute(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM {_TABLE}
                GROUP BY tenant_id, session_id, channel, source_key
                HAVING COUNT(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 0044: remove extra saved persona skills first';
            END IF;
        END
        $$
        """
    )
    op.execute(
        "UPDATE app_schema_contract SET compatibility_level = 6 "
        "WHERE contract_name = 'agent-console-runtime'"
    )
    op.drop_index(_SCOPE_SKILL_INDEX, table_name=_TABLE)
    op.drop_index(_ACTIVE_SCOPE_INDEX, table_name=_TABLE)
    op.create_unique_constraint(
        _LEGACY_SCOPE_CONSTRAINT,
        _TABLE,
        ["tenant_id", "session_id", "channel", "source_key"],
    )
