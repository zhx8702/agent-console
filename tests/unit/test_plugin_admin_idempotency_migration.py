from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.migration import MigrationContext
from alembic.operations import Operations


def _render(monkeypatch, operation: str) -> tuple[object, str]:
    migration = import_module(
        "migrations.versions.20260718_0026_plugin_admin_idempotency"
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, operation)()
    return migration, output.getvalue()


def test_plugin_admin_idempotency_migration_is_linear_and_secret_free(
    monkeypatch,
) -> None:
    migration, rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0026_plugin_admin_idempotency"
    assert migration.down_revision == "0025_runtime_llm_config"
    assert "CREATE TABLE plugin_lifecycle_operation" in rendered
    assert "idempotency_key_hash VARCHAR(64) NOT NULL" in rendered
    assert "request_fingerprint VARCHAR(64) NOT NULL" in rendered
    assert "result_json JSONB" in rendered
    assert "before_state_json JSONB" in rendered
    assert "after_state_json JSONB" in rendered
    assert "ix_plugin_lifecycle_operation_plugin_status" in rendered
    assert "idempotency_key VARCHAR" not in rendered
    assert "request_body" not in rendered


def test_plugin_admin_idempotency_migration_downgrades_reversibly(
    monkeypatch,
) -> None:
    _migration, rendered = _render(monkeypatch, "downgrade")

    assert "DROP INDEX ix_plugin_lifecycle_operation_updated" in rendered
    assert "DROP INDEX ix_plugin_lifecycle_operation_plugin_status" in rendered
    assert "DROP TABLE plugin_lifecycle_operation" in rendered
