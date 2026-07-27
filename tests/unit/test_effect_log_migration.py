"""Schema contract tests for the migration-owned flow effect log."""

from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_effect_log_migration_renders_offline_without_inspection(monkeypatch) -> None:
    migration = import_module(
        "migrations.versions.20260717_0012_flow_effect_state_machine"
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))

    migration.upgrade()

    rendered = output.getvalue()
    assert "CREATE TABLE flow_effect_log" in rendered
    assert "ix_flow_effect_log_status_lease" in rendered
    assert "ix_flow_effect_log_tenant_created" in rendered


def test_effect_log_state_machine_migration_creates_required_structure(monkeypatch) -> None:
    migration = import_module("migrations.versions.20260717_0012_flow_effect_state_machine")
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            monkeypatch.setattr(migration, "op", operations)

            migration.upgrade()

            inspector = inspect(connection)
            columns = {str(column["name"]) for column in inspector.get_columns("flow_effect_log")}
            unique_columns = {
                frozenset(str(value) for value in constraint.get("column_names") or [])
                for constraint in inspector.get_unique_constraints("flow_effect_log")
            }
            checks = " ".join(
                str(constraint.get("sqltext") or "").lower()
                for constraint in inspector.get_check_constraints("flow_effect_log")
            )
            indexes = {
                str(index.get("name") or "") for index in inspector.get_indexes("flow_effect_log")
            }
    finally:
        engine.dispose()

    assert migration.revision == "0012_flow_effect_state_machine"
    assert migration.down_revision == "0011_session_tenant_scope_expand"
    assert {
        "status",
        "claim_owner",
        "lease_expires_at",
        "attempt",
        "last_error",
        "updated_at",
        "started_at",
        "completed_at",
        "failed_at",
    }.issubset(columns)
    assert frozenset({"tenant_id", "idempotency_key", "dry_run"}) in unique_columns
    assert all(status in checks for status in ("prepared", "running", "completed", "failed"))
    assert "ix_flow_effect_log_status_lease" in indexes
    assert "ix_flow_effect_log_tenant_created" in indexes


def test_effect_log_migration_adopts_runtime_created_sqlite_table(monkeypatch) -> None:
    migration = import_module("migrations.versions.20260717_0012_flow_effect_state_machine")
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE flow_effect_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        tenant_id TEXT NOT NULL DEFAULT '',
                        session_id TEXT NOT NULL DEFAULT '',
                        trace_id TEXT NOT NULL DEFAULT '',
                        owner TEXT NOT NULL,
                        type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        dry_run INTEGER NOT NULL DEFAULT 0,
                        payload TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO flow_effect_log (
                        idempotency_key, tenant_id, session_id, trace_id,
                        owner, type, status, dry_run, payload
                    ) VALUES (
                        'legacy-key', 'tenant-1', 'session-1', 'trace-1',
                        'core', 'publish_outbound', 'recorded', 0, '{}'
                    )
                    """
                )
            )
            operations = Operations(MigrationContext.configure(connection))
            monkeypatch.setattr(migration, "op", operations)

            migration.upgrade()

            row = (
                connection.execute(
                    text(
                        """
                    SELECT status, claim_owner, lease_expires_at, attempt,
                           completed_at, updated_at
                    FROM flow_effect_log
                    WHERE idempotency_key = 'legacy-key'
                    """
                    )
                )
                .mappings()
                .one()
            )
            # The composite uniqueness must allow a dry-run reconciliation row
            # without consuming the production execution key.
            connection.execute(
                text(
                    """
                    INSERT INTO flow_effect_log (
                        id, idempotency_key, tenant_id, session_id, trace_id,
                        owner, type, status, dry_run, payload
                    ) VALUES (
                        2, 'legacy-key', 'tenant-1', 'session-1', 'trace-2',
                        'core', 'publish_outbound', 'completed', 1, '{}'
                    )
                    """
                )
            )
            # The same explicit command is a distinct production effect in a
            # different tenant.
            connection.execute(
                text(
                    """
                    INSERT INTO flow_effect_log (
                        id, idempotency_key, tenant_id, session_id, trace_id,
                        owner, type, status, dry_run, payload
                    ) VALUES (
                        3, 'legacy-key', 'tenant-2', 'session-2', 'trace-3',
                        'core', 'publish_outbound', 'completed', 0, '{}'
                    )
                    """
                )
            )
            count = connection.scalar(
                text("SELECT count(*) FROM flow_effect_log WHERE idempotency_key = 'legacy-key'")
            )
    finally:
        engine.dispose()

    assert row["status"] == "completed"
    assert row["claim_owner"] == ""
    assert row["lease_expires_at"] is None
    assert row["attempt"] == 0
    assert row["completed_at"]
    assert row["updated_at"]
    assert count == 3
