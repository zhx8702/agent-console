from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.migration import MigrationContext
from alembic.operations import Operations


def _render(monkeypatch, operation: str) -> tuple[object, str]:
    migration = import_module(
        "migrations.versions.20260718_0029_wxbot_admin_mutation_state"
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, operation)()
    return migration, output.getvalue()


def test_wxbot_admin_mutation_migration_is_linear_durable_and_secret_free(
    monkeypatch,
) -> None:
    migration, rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0029_wxbot_admin_mutation_state"
    assert migration.down_revision == "0028_plugin_mutation_ledger"
    assert "CREATE TABLE plugin_wxbot_admin_resource_version" in rendered
    assert "CREATE TABLE plugin_wxbot_admin_mutation_state" in rendered
    assert "pending_mutation_id VARCHAR(36)" in rendered
    assert "idempotency_key_hash VARCHAR(64) NOT NULL" in rendered
    assert "request_hash VARCHAR(64) NOT NULL" in rendered
    assert "recovery_response_json JSON" in rendered
    assert "uq_plugin_wxbot_admin_mutation_key" in rendered
    assert "ix_wxbot_admin_mutation_status_updated" in rendered
    assert "ix_wxbot_admin_mutation_resource" in rendered
    assert "idempotency_key VARCHAR" not in rendered
    assert "request_body" not in rendered


def test_wxbot_admin_mutation_migration_downgrades_reversibly(monkeypatch) -> None:
    _migration, rendered = _render(monkeypatch, "downgrade")

    assert "DROP INDEX ix_wxbot_admin_mutation_resource" in rendered
    assert "DROP INDEX ix_wxbot_admin_mutation_status_updated" in rendered
    assert "DROP TABLE plugin_wxbot_admin_mutation_state" in rendered
    assert "DROP INDEX ix_wxbot_admin_resource_pending" in rendered
    assert "DROP TABLE plugin_wxbot_admin_resource_version" in rendered
