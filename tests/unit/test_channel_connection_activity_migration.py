from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.channel_connection import ChannelConnectionRow


def _render(monkeypatch, operation: str) -> str:
    migration = import_module(
        "migrations.versions.20260720_0039_channel_connection_activity"
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, operation)()
    return output.getvalue()


def test_channel_connection_activity_migration_is_linear_and_truthful(monkeypatch) -> None:
    migration = import_module(
        "migrations.versions.20260720_0039_channel_connection_activity"
    )
    rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0039_channel_connection_activity"
    assert migration.down_revision == "0038_wechat_sdk_no_token"
    assert "ADD COLUMN last_inbound_at TIMESTAMP WITH TIME ZONE" in rendered
    assert "ADD COLUMN last_outbound_delivered_at TIMESTAMP WITH TIME ZONE" in rendered
    assert "WHERE status = 'sent'" in rendered
    assert "MAX(sent_at)" in rendered


def test_channel_connection_activity_model_matches_migration() -> None:
    table = ChannelConnectionRow.__table__
    assert table.c.last_inbound_at.nullable is True
    assert table.c.last_outbound_delivered_at.nullable is True
