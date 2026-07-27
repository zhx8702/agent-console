from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.migration import MigrationContext
from alembic.operations import Operations


def _render(monkeypatch, operation: str) -> tuple[object, str]:
    migration = import_module(
        "migrations.versions.20260718_0035_plugin_lifecycle_global_index"
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, operation)()
    return migration, output.getvalue()


def test_plugin_lifecycle_global_queue_index_upgrade_and_downgrade(monkeypatch) -> None:
    migration, upgrade = _render(monkeypatch, "upgrade")
    _migration, downgrade = _render(monkeypatch, "downgrade")

    assert migration.down_revision == "0034_message_effect_producer_owner"
    assert "ix_plugin_lifecycle_in_progress_created" in upgrade
    assert "WHERE status = 'in_progress'" in upgrade
    assert "DROP INDEX ix_plugin_lifecycle_in_progress_created" in downgrade
