from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from app.infra.runtime_schema import RUNTIME_SCHEMA_REVISION


def _render(monkeypatch, operation: str) -> str:
    migration = import_module("migrations.versions.20260718_0031_memory_audience_dedupe")
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, operation)()
    return output.getvalue()


def test_memory_audience_dedupe_migration_remains_on_the_single_head_chain(
    monkeypatch,
) -> None:
    migration = import_module("migrations.versions.20260718_0031_memory_audience_dedupe")
    rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0031_memory_audience_dedupe"
    assert migration.down_revision == "0030_plugin_scope_concurrency"
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == [RUNTIME_SCHEMA_REVISION]
    assert script.get_revision("0032_channel_connections").down_revision == migration.revision
    assert "DROP INDEX IF EXISTS ux_memory_item_dedupe" in rendered
    assert "origin_session_kind" in rendered
    assert "audience_scope" in rendered
    assert "md5(allowed_session_ids::text)" in rendered
    assert "WHERE deleted_at IS NULL" in rendered


def test_memory_audience_dedupe_migration_downgrade_restores_legacy_key(
    monkeypatch,
) -> None:
    rendered = _render(monkeypatch, "downgrade")

    assert "DROP INDEX IF EXISTS ux_memory_item_dedupe" in rendered
    assert "source_type, normalized_key)" in rendered
    assert "origin_session_kind" not in rendered
    assert "md5(" not in rendered
