"""Remove the invented WeChat SDK connection credential.

Revision ID: 0038_wechat_sdk_no_token
Revises: 0037_runtime_hardening_merge
Create Date: 2026-07-20
"""

from __future__ import annotations

from alembic import op

revision = "0038_wechat_sdk_no_token"
down_revision = "0037_runtime_hardening_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_channel_connection_secret_status",
        "channel_connection",
        type_="check",
    )
    op.create_check_constraint(
        "ck_channel_connection_secret_status",
        "channel_connection",
        "secret_status IN ('missing', 'reference_configured', 'not_required')",
    )
    op.execute(
        """
        UPDATE channel_connection
        SET secret_ref = '',
            secret_status = 'not_required',
            secret_fingerprint = '',
            effective_state = CASE
                WHEN desired_state = 'enabled' THEN 'unverified'
                ELSE effective_state
            END,
            last_probed_at = NULL,
            last_probe_status = '',
            last_error_code = '',
            version = version + 1,
            updated_at = NOW()
        WHERE adapter_id = 'wechat-sdk'
          AND (
              secret_ref <> ''
              OR secret_status <> 'not_required'
              OR secret_fingerprint <> ''
          )
        """
    )


def downgrade() -> None:
    # Removed references do not contain the secret value and cannot be
    # reconstructed safely. Downgrade only restores the old status vocabulary.
    op.execute(
        """
        UPDATE channel_connection
        SET secret_status = 'missing'
        WHERE secret_status = 'not_required'
        """
    )
    op.drop_constraint(
        "ck_channel_connection_secret_status",
        "channel_connection",
        type_="check",
    )
    op.create_check_constraint(
        "ck_channel_connection_secret_status",
        "channel_connection",
        "secret_status IN ('missing', 'reference_configured')",
    )
