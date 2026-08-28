from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from app.infra.runtime_schema import (
    RUNTIME_SCHEMA_COMPATIBILITY_LEVEL,
    RUNTIME_SCHEMA_REVISION,
)

_MODULE = "migrations.versions.20260718_0034_message_effect_producer_owner"


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


def test_message_effect_producer_owner_upgrade_is_linear_and_backfilled(
    monkeypatch,
) -> None:
    migration, rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0034_message_effect_producer_owner"
    assert migration.down_revision == "0033_wxbot_event_connection_scope"
    assert RUNTIME_SCHEMA_REVISION == "0049_speaker_portrait_hot_update"
    assert RUNTIME_SCHEMA_COMPATIBILITY_LEVEL == 9
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == [RUNTIME_SCHEMA_REVISION]
    assert (
        script.get_revision("0035_plugin_lifecycle_global_index").down_revision
        == migration.revision
    )
    assert "ADD COLUMN producer_owner VARCHAR(128)" in rendered
    assert "drain executable effect intents" in rendered
    assert "status IN ('prepared', 'running')" in rendered
    assert "status = 'failed' AND available_at IS NOT NULL" in rendered
    assert "SET producer_owner = owner" in rendered
    assert "ALTER COLUMN producer_owner SET NOT NULL" in rendered
    assert "compatibility_level = 3" in rendered


def test_message_effect_producer_owner_downgrade_requires_drained_intents(
    monkeypatch,
) -> None:
    _migration, rendered = _render(monkeypatch, "downgrade")

    lock = "LOCK TABLE message_effect_intent IN ACCESS EXCLUSIVE MODE"
    assert lock in rendered
    assert rendered.index(lock) < rendered.index("DO $$")
    assert rendered.index("DO $$") < rendered.index("DROP COLUMN producer_owner")
    assert "pending effect intents require producer provenance" in rendered
    assert "producer_owner <> owner" in rendered
    assert "status IN ('prepared', 'running')" in rendered
    assert "status = 'failed' AND available_at IS NOT NULL" in rendered
    assert "compatibility_level = 2" in rendered
    assert "DROP COLUMN producer_owner" in rendered
