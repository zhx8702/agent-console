from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.session import SessionRow


def _render(monkeypatch, operation: str) -> str:
    migration = import_module("migrations.versions.20260718_0032_channel_connections")
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, operation)()
    return output.getvalue()


def test_channel_connection_migration_is_linear_and_reference_only(monkeypatch) -> None:
    migration = import_module("migrations.versions.20260718_0032_channel_connections")
    rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0032_channel_connections"
    assert migration.down_revision == "0031_memory_audience_dedupe"
    assert "ALTER TABLE sessions ALTER COLUMN channel TYPE VARCHAR(64)" in rendered
    assert (
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN connection_id "
        "VARCHAR(64) DEFAULT '' NOT NULL"
    ) in rendered
    assert "agent_console_try_jsonb_0032(delivery_json::text)" in rendered
    assert "jsonb_typeof(parsed.payload -> 'connection_id') = 'string'" in rendered
    assert "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$" in rendered
    assert "ELSE 'legacy-wechat-default'" in rendered
    assert (
        "plugin_wxbot_reply_queue (tenant_id, connection_id, status, not_before, created_at, id)"
    ) in rendered
    assert (
        "CREATE UNIQUE INDEX idx_wxbot_reply_queue_tenant_command_id_unique "
        "ON plugin_wxbot_reply_queue (tenant_id, connection_id, command_id) "
        "WHERE command_id <> ''"
    ) in rendered
    assert "CREATE TABLE channel_connection" in rendered
    assert "PRIMARY KEY (tenant_id, connection_id)" in rendered
    assert "secret_ref VARCHAR(512)" in rendered
    assert "secret_fingerprint VARCHAR(64)" in rendered
    assert "desired_state IN ('draft', 'disabled', 'enabled')" in rendered
    assert "api_token" not in rendered.lower()
    assert "password" not in rendered.lower()


def test_channel_connection_migration_downgrade_is_reversible(monkeypatch) -> None:
    rendered = _render(monkeypatch, "downgrade")

    assert "DROP TABLE channel_connection" in rendered
    assert "DROP INDEX idx_wxbot_reply_queue_connection_claim" in rendered
    assert (
        "ON plugin_wxbot_reply_queue (tenant_id, command_id) WHERE command_id <> ''"
    ) in rendered
    assert "command ids overlap across connections" in rendered
    assert "DROP COLUMN connection_id" in rendered
    assert "ALTER TABLE sessions ALTER COLUMN channel TYPE VARCHAR(32)" in rendered


def test_session_channel_model_matches_adapter_identifier_width() -> None:
    assert SessionRow.__table__.c.channel.type.length == 64
