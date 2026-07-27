"""Persist observed channel connection activity.

Revision ID: 0039_channel_connection_activity
Revises: 0038_wechat_sdk_no_token
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039_channel_connection_activity"
down_revision = "0038_wechat_sdk_no_token"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "channel_connection",
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "channel_connection",
        sa.Column(
            "last_outbound_delivered_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # A final SDK delivery acknowledgement is durable proof, unlike an
    # outbound Redis entry or a reply that has only reached the plugin queue.
    op.execute(
        """
        UPDATE channel_connection AS connection
        SET last_outbound_delivered_at = delivered.last_delivered_at
        FROM (
            SELECT tenant_id, connection_id, MAX(sent_at) AS last_delivered_at
            FROM plugin_wxbot_reply_queue
            WHERE status = 'sent'
              AND sent_at IS NOT NULL
              AND connection_id <> ''
            GROUP BY tenant_id, connection_id
        ) AS delivered
        WHERE connection.tenant_id = delivered.tenant_id
          AND connection.connection_id = delivered.connection_id
        """
    )


def downgrade() -> None:
    op.drop_column("channel_connection", "last_outbound_delivered_at")
    op.drop_column("channel_connection", "last_inbound_at")
