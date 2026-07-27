"""make flow effect execution claims durable and retryable

Revision ID: 0012_flow_effect_state_machine
Revises: 0011_session_tenant_scope_expand
Create Date: 2026-07-17

The table previously existed only through runtime ``CREATE TABLE IF NOT
EXISTS``.  This migration adopts those installations, maps legacy terminal
labels to ``completed``, adds fenced owner/lease/attempt transitions, and
scopes effect identity to ``(tenant_id, idempotency_key, dry_run)``.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0012_flow_effect_state_machine"
down_revision = "0011_session_tenant_scope_expand"
branch_labels = None
depends_on = None

_TABLE = "flow_effect_log"
_STATUSES = ("prepared", "running", "completed", "failed")


def upgrade() -> None:
    # Offline SQL generation represents the deterministic fresh-schema path
    # and cannot inspect a live database. Legacy runtime-table adoption is
    # intentionally reserved for online migrations.
    if bool(getattr(op.get_context(), "as_sql", False)):
        _create_table(_TABLE)
        _create_indexes_without_inspection()
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        _create_table(_TABLE)
    else:
        _adopt_runtime_table(inspector)
    _create_indexes()


def downgrade() -> None:
    op.drop_index("ix_flow_effect_log_tenant_created", table_name=_TABLE)
    op.drop_index("ix_flow_effect_log_status_lease", table_name=_TABLE)
    op.drop_table(_TABLE)


def _create_table(table_name: str) -> None:
    op.create_table(
        table_name,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("session_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("trace_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="prepared"),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("claim_owner", sa.Text(), nullable=False, server_default=""),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"status IN {_STATUSES!r}",
            name="ck_flow_effect_log_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_flow_effect_log_attempt"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            "dry_run",
            name="uq_flow_effect_log_tenant_key_dry",
        ),
    )


def _adopt_runtime_table(inspector: sa.Inspector) -> None:
    if op.get_bind().dialect.name == "sqlite":
        _adopt_sqlite_runtime_table()
        return

    columns = {str(column["name"]) for column in inspector.get_columns(_TABLE)}
    additions: dict[str, sa.Column[Any]] = {
        "claim_owner": sa.Column(
            "claim_owner",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        "lease_expires_at": sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        "attempt": sa.Column(
            "attempt",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        "last_error": sa.Column(
            "last_error",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        "updated_at": sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        "started_at": sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        "completed_at": sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        "failed_at": sa.Column(
            "failed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column(_TABLE, column)

    op.execute(
        sa.text(
            """
            UPDATE flow_effect_log
            SET status = CASE
                    WHEN status IN ('recorded', 'dry_run', 'duplicate') THEN 'completed'
                    WHEN status IN ('prepared', 'running', 'completed', 'failed') THEN status
                    ELSE 'failed'
                END,
                completed_at = CASE
                    WHEN status IN ('recorded', 'dry_run', 'duplicate', 'completed')
                    THEN COALESCE(completed_at, created_at)
                    ELSE completed_at
                END,
                last_error = CASE
                    WHEN status IN (
                        'recorded', 'dry_run', 'duplicate',
                        'prepared', 'running', 'completed', 'failed'
                    ) THEN last_error
                    ELSE 'legacy_unknown_status'
                END,
                updated_at = COALESCE(updated_at, created_at)
            """
        )
    )

    unique_constraints = inspector.get_unique_constraints(_TABLE)
    tenant_identity_columns = {"tenant_id", "idempotency_key", "dry_run"}
    obsolete_identity_columns = (
        {"idempotency_key"},
        {"idempotency_key", "dry_run"},
    )
    has_tenant_unique = any(
        set(constraint.get("column_names") or []) == tenant_identity_columns
        for constraint in unique_constraints
    )
    for constraint in unique_constraints:
        if set(constraint.get("column_names") or []) not in obsolete_identity_columns:
            continue
        constraint_name = constraint.get("name")
        if constraint_name:
            op.drop_constraint(str(constraint_name), _TABLE, type_="unique")

    for index in inspector.get_indexes(_TABLE):
        if not index.get("unique"):
            continue
        if index.get("duplicates_constraint"):
            continue
        if set(index.get("column_names") or []) not in obsolete_identity_columns:
            continue
        index_name = str(index.get("name") or "")
        if index_name:
            op.drop_index(index_name, table_name=_TABLE)

    if not has_tenant_unique:
        op.create_unique_constraint(
            "uq_flow_effect_log_tenant_key_dry",
            _TABLE,
            ["tenant_id", "idempotency_key", "dry_run"],
        )

    check_names = {
        str(constraint.get("name") or "") for constraint in inspector.get_check_constraints(_TABLE)
    }
    if "ck_flow_effect_log_status" not in check_names:
        op.create_check_constraint(
            "ck_flow_effect_log_status",
            _TABLE,
            f"status IN {_STATUSES!r}",
        )
    if "ck_flow_effect_log_attempt" not in check_names:
        op.create_check_constraint(
            "ck_flow_effect_log_attempt",
            _TABLE,
            "attempt >= 0",
        )


def _adopt_sqlite_runtime_table() -> None:
    """Rebuild SQLite's unnamed single-key UNIQUE constraint for tests/tools."""

    replacement = "flow_effect_log_state_machine_new"
    _create_table(replacement)
    op.execute(
        sa.text(
            f"""
            INSERT INTO {replacement} (
                id, idempotency_key, tenant_id, session_id, trace_id, owner,
                type, status, dry_run, payload, claim_owner,
                lease_expires_at, attempt, last_error, created_at, updated_at,
                started_at, completed_at, failed_at
            )
            SELECT
                id, idempotency_key, tenant_id, session_id, trace_id, owner,
                type,
                CASE
                    WHEN status IN ('recorded', 'dry_run', 'duplicate') THEN 'completed'
                    WHEN status IN ('prepared', 'running', 'completed', 'failed') THEN status
                    ELSE 'failed'
                END,
                dry_run, payload, '', NULL, 0,
                CASE
                    WHEN status IN (
                        'recorded', 'dry_run', 'duplicate',
                        'prepared', 'running', 'completed', 'failed'
                    ) THEN ''
                    ELSE 'legacy_unknown_status'
                END,
                created_at, created_at, NULL,
                CASE
                    WHEN status IN ('recorded', 'dry_run', 'duplicate', 'completed')
                    THEN created_at
                    ELSE NULL
                END,
                CASE WHEN status = 'failed' THEN created_at ELSE NULL END
            FROM {_TABLE}
            """
        )
    )
    op.drop_table(_TABLE)
    op.rename_table(replacement, _TABLE)


def _create_indexes() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    index_names = {str(index.get("name") or "") for index in inspector.get_indexes(_TABLE)}
    if "ix_flow_effect_log_status_lease" not in index_names:
        op.create_index(
            "ix_flow_effect_log_status_lease",
            _TABLE,
            ["status", "lease_expires_at"],
        )
    if "ix_flow_effect_log_tenant_created" not in index_names:
        op.create_index(
            "ix_flow_effect_log_tenant_created",
            _TABLE,
            ["tenant_id", "created_at"],
        )


def _create_indexes_without_inspection() -> None:
    op.create_index(
        "ix_flow_effect_log_status_lease",
        _TABLE,
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "ix_flow_effect_log_tenant_created",
        _TABLE,
        ["tenant_id", "created_at"],
    )
