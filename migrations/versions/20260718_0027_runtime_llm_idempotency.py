"""Add durable exact replay for runtime LLM configuration writes.

Revision ID: 0027_runtime_llm_idempotency
Revises: 0026_plugin_admin_idempotency
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027_runtime_llm_idempotency"
down_revision = "0026_plugin_admin_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_llm_config_history",
        sa.Column("version", sa.Integer(), nullable=False),
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
            "version > 0",
            name="ck_runtime_llm_config_history_version_positive",
        ),
        sa.PrimaryKeyConstraint("version", name="pk_runtime_llm_config_history"),
    )
    op.create_table(
        "runtime_llm_config_idempotency",
        # Only one-way digests are retained. Raw idempotency keys and request /
        # response bodies never enter this table.
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("result_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["result_version"],
            ["runtime_llm_config_history.version"],
            name="fk_runtime_llm_idempotency_result_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "key_hash",
            name="pk_runtime_llm_config_idempotency",
        ),
    )


def downgrade() -> None:
    op.drop_table("runtime_llm_config_idempotency")
    op.drop_table("runtime_llm_config_history")
