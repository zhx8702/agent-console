from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.migration import MigrationContext
from alembic.operations import Operations


def _render(monkeypatch, operation: str) -> tuple[object, str]:
    migration = import_module("migrations.versions.20260718_0024_reply_policy_atomicity")
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, operation)()
    return migration, output.getvalue()


def test_reply_policy_atomicity_migration_upgrades_all_owned_state(
    monkeypatch,
) -> None:
    migration, rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0024_reply_policy_atomicity"
    assert migration.down_revision == "0023_plugin_config_versions"
    for table_name in (
        "plugin_wxbot_tenant_policy",
        "plugin_wxbot_session_policy",
        "plugin_repeater_config",
    ):
        assert f"ALTER TABLE {table_name} ADD COLUMN version INTEGER" in rendered
    assert "CREATE TABLE plugin_wxbot_reply_policy_aggregate_state" in rendered
    assert "CREATE TABLE plugin_wxbot_reply_policy_idempotency" in rendered
    assert "request_hash VARCHAR(64) NOT NULL" in rendered
    assert "response_json JSONB DEFAULT '{}'::jsonb NOT NULL" in rendered
    assert "ix_wxbot_reply_aggregate_effect" in rendered
    assert "ix_wxbot_reply_policy_idempotency_created" in rendered


def test_reply_policy_atomicity_migration_downgrades_reversibly(
    monkeypatch,
) -> None:
    _migration, rendered = _render(monkeypatch, "downgrade")

    assert "DROP TABLE plugin_wxbot_reply_policy_idempotency" in rendered
    assert "DROP TABLE plugin_wxbot_reply_policy_aggregate_state" in rendered
    for table_name in (
        "plugin_repeater_config",
        "plugin_wxbot_session_policy",
        "plugin_wxbot_tenant_policy",
    ):
        assert f"ALTER TABLE {table_name} DROP COLUMN version" in rendered
