"""Add durable, secret-free runtime LLM configuration overrides.

Revision ID: 0025_runtime_llm_config
Revises: 0024_reply_policy_atomicity
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025_runtime_llm_config"
down_revision = "0024_reply_policy_atomicity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_llm_config",
        sa.Column("config_key", sa.String(length=32), nullable=False),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "overrides_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "version >= 0",
            name="ck_runtime_llm_config_version_nonnegative",
        ),
        sa.PrimaryKeyConstraint("config_key", name="pk_runtime_llm_config"),
    )


def downgrade() -> None:
    op.drop_table("runtime_llm_config")
