from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.migration import MigrationContext
from alembic.operations import Operations


def _render(monkeypatch, operation: str) -> str:
    migration = import_module(
        "migrations.versions.20260718_0030_plugin_scope_concurrency"
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, operation)()
    return output.getvalue()


def test_plugin_scope_concurrency_migration_is_linear_and_versioned(monkeypatch) -> None:
    migration = import_module(
        "migrations.versions.20260718_0030_plugin_scope_concurrency"
    )
    rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0030_plugin_scope_concurrency"
    assert migration.down_revision == "0029_wxbot_admin_mutation_state"
    assert "ALTER TABLE plugin_scope_state ADD COLUMN version INTEGER" in rendered
    assert "ck_plugin_scope_state_version_positive" in rendered
    assert "compatibility_level = 2" in rendered


def test_plugin_scope_concurrency_migration_downgrade_is_reversible(monkeypatch) -> None:
    rendered = _render(monkeypatch, "downgrade")

    assert "ck_plugin_scope_state_version_positive" in rendered
    assert "ALTER TABLE plugin_scope_state DROP COLUMN version" in rendered
    assert "compatibility_level = 1" in rendered
