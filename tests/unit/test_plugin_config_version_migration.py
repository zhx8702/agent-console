from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.migration import MigrationContext
from alembic.operations import Operations


def _render(monkeypatch, operation: str) -> str:
    migration = import_module(
        "migrations.versions.20260718_0023_plugin_config_versions"
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, operation)()
    return output.getvalue()


def test_plugin_config_version_migration_is_linear_and_adds_all_versions(
    monkeypatch,
) -> None:
    migration = import_module(
        "migrations.versions.20260718_0023_plugin_config_versions"
    )
    rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0023_plugin_config_versions"
    assert migration.down_revision == "0022_social_scope_controls"
    for table_name in (
        "plugin_command_center_config",
        "plugin_moderation_config",
        "plugin_group_activity_config",
    ):
        assert f"ALTER TABLE {table_name} ADD COLUMN version INTEGER" in rendered
        assert f"ck_{table_name}_version_positive" in rendered
    assert "INSERT INTO plugin_moderation_config" in rendered
    assert "FROM plugin_moderation_keywords" in rendered


def test_plugin_config_version_migration_downgrade_is_reversible(monkeypatch) -> None:
    rendered = _render(monkeypatch, "downgrade")

    for table_name in (
        "plugin_command_center_config",
        "plugin_moderation_config",
        "plugin_group_activity_config",
    ):
        assert f"ALTER TABLE {table_name} DROP COLUMN version" in rendered
