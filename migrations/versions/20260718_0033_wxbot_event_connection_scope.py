"""Scope durable wxbot member and media events by connection.

Revision ID: 0033_wxbot_event_connection_scope
Revises: 0032_channel_connections
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_wxbot_event_connection_scope"
down_revision = "0032_channel_connections"
branch_labels = None
depends_on = None

_LEGACY_CONNECTION_ID = "legacy-wechat-default"


def _add_connection_scope(
    *,
    table_name: str,
    unique_index_name: str,
    created_index_name: str,
    check_constraint_name: str,
) -> None:
    op.add_column(
        table_name,
        sa.Column(
            "connection_id",
            sa.String(length=64),
            nullable=False,
            server_default=_LEGACY_CONNECTION_ID,
        ),
    )
    # The pre-0033 schema did not persist a trustworthy connection identity.
    # Conservatively assign every historical row to the reserved legacy scope;
    # never infer a security boundary from provider-controlled payload JSON.
    op.execute(
        f"UPDATE {table_name} "
        f"SET connection_id = '{_LEGACY_CONNECTION_ID}' "
        "WHERE btrim(connection_id) = '' "
        f"OR connection_id = '{_LEGACY_CONNECTION_ID}'"
    )
    op.create_check_constraint(
        check_constraint_name,
        table_name,
        "connection_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'",
    )
    op.drop_index(unique_index_name, table_name=table_name)
    op.drop_index(created_index_name, table_name=table_name)
    op.create_index(
        unique_index_name,
        table_name,
        ["tenant_id", "connection_id", "sdk_event_id"],
        unique=True,
    )
    op.create_index(
        created_index_name,
        table_name,
        [
            "tenant_id",
            "connection_id",
            sa.text("created_ts DESC"),
            sa.text("id DESC"),
        ],
    )


def upgrade() -> None:
    _add_connection_scope(
        table_name="plugin_wxbot_member_events",
        unique_index_name="idx_wxbot_member_events_tenant_sdk_event_id_unique",
        created_index_name="idx_wxbot_member_events_tenant_created",
        check_constraint_name="ck_wxbot_member_events_connection_id",
    )
    _add_connection_scope(
        table_name="plugin_wxbot_media_ready_events",
        unique_index_name="idx_wxbot_media_ready_events_tenant_sdk_event_id_unique",
        created_index_name="idx_wxbot_media_ready_events_tenant_created",
        check_constraint_name="ck_wxbot_media_ready_events_connection_id",
    )


def _restore_legacy_scope(
    *,
    table_name: str,
    unique_index_name: str,
    created_index_name: str,
    check_constraint_name: str,
) -> None:
    op.drop_index(unique_index_name, table_name=table_name)
    op.drop_index(created_index_name, table_name=table_name)
    op.drop_constraint(check_constraint_name, table_name=table_name, type_="check")
    op.drop_column(table_name, "connection_id")
    op.create_index(
        unique_index_name,
        table_name,
        ["tenant_id", "sdk_event_id"],
        unique=True,
    )
    op.create_index(
        created_index_name,
        table_name,
        ["tenant_id", sa.text("created_ts DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    # Restoring the old tenant-only uniqueness contract would fail or silently
    # collapse distinct events when two connections reused an SDK event id.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM plugin_wxbot_member_events
                GROUP BY tenant_id, sdk_event_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 0033: member event ids overlap across connections';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM plugin_wxbot_media_ready_events
                GROUP BY tenant_id, sdk_event_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 0033: media event ids overlap across connections';
            END IF;
        END
        $$
        """
    )
    _restore_legacy_scope(
        table_name="plugin_wxbot_media_ready_events",
        unique_index_name="idx_wxbot_media_ready_events_tenant_sdk_event_id_unique",
        created_index_name="idx_wxbot_media_ready_events_tenant_created",
        check_constraint_name="ck_wxbot_media_ready_events_connection_id",
    )
    _restore_legacy_scope(
        table_name="plugin_wxbot_member_events",
        unique_index_name="idx_wxbot_member_events_tenant_sdk_event_id_unique",
        created_index_name="idx_wxbot_member_events_tenant_created",
        check_constraint_name="ck_wxbot_member_events_connection_id",
    )
