from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from app.infra.runtime_schema import RUNTIME_SCHEMA_REVISION

_MODULE = "migrations.versions.20260718_0033_wxbot_event_connection_scope"


def _render(monkeypatch, operation: str) -> tuple[object, str]:
    migration = import_module(_MODULE)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, operation)()
    return migration, output.getvalue()


def test_wxbot_event_connection_scope_migration_is_linear_and_guarded(monkeypatch) -> None:
    migration, rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0033_wxbot_event_connection_scope"
    assert migration.down_revision == "0032_channel_connections"
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == [RUNTIME_SCHEMA_REVISION]
    assert (
        script.get_revision("0034_message_effect_producer_owner").down_revision
        == migration.revision
    )
    assert rendered.count("ADD COLUMN connection_id VARCHAR(64)") == 2
    assert rendered.count("DEFAULT 'legacy-wechat-default' NOT NULL") == 2
    assert rendered.count("SET connection_id = 'legacy-wechat-default'") == 2
    assert "ck_wxbot_member_events_connection_id" in rendered
    assert "ck_wxbot_media_ready_events_connection_id" in rendered
    assert "plugin_wxbot_member_events (tenant_id, connection_id, sdk_event_id)" in rendered
    assert "plugin_wxbot_media_ready_events (tenant_id, connection_id, sdk_event_id)" in rendered
    assert (
        "plugin_wxbot_member_events (tenant_id, connection_id, created_ts DESC, id DESC)"
        in rendered
    )
    assert (
        "plugin_wxbot_media_ready_events (tenant_id, connection_id, created_ts DESC, id DESC)"
        in rendered
    )


def test_wxbot_event_connection_scope_downgrade_fails_closed_on_overlap(
    monkeypatch,
) -> None:
    _migration, rendered = _render(monkeypatch, "downgrade")

    assert "member event ids overlap across connections" in rendered
    assert "media event ids overlap across connections" in rendered
    assert rendered.count("DROP COLUMN connection_id") == 2
    assert "plugin_wxbot_member_events (tenant_id, sdk_event_id)" in rendered
    assert "plugin_wxbot_media_ready_events (tenant_id, sdk_event_id)" in rendered
    assert "plugin_wxbot_member_events (tenant_id, created_ts DESC, id DESC)" in rendered
    assert "plugin_wxbot_media_ready_events (tenant_id, created_ts DESC, id DESC)" in rendered
