from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from app.infra.runtime_schema import (
    RUNTIME_SCHEMA_COLUMN_CONTRACTS,
    RUNTIME_SCHEMA_INDEXES,
    RUNTIME_SCHEMA_REVISION,
)

_MODULE = "migrations.versions.20260902_0050_speaker_portrait_incremental_cursor"


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


def test_speaker_portrait_cursor_upgrade_is_runtime_head(monkeypatch) -> None:
    migration, rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0050_speaker_portrait_cursor"
    assert migration.down_revision == "0049_speaker_portrait_hot_update"
    assert RUNTIME_SCHEMA_REVISION == migration.revision
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_heads() == [
        RUNTIME_SCHEMA_REVISION
    ]
    assert "ADD COLUMN IF NOT EXISTS last_distilled_message_at" in rendered
    assert "ADD COLUMN IF NOT EXISTS claimed_pending_messages" in rendered
    assert "CREATE INDEX IF NOT EXISTS idx_speaker_portrait_job_attempt" in rendered
    assert (
        "plugin_speaker_portraits",
        "last_distilled_message_at",
        False,
    ) in RUNTIME_SCHEMA_COLUMN_CONTRACTS
    assert (
        "plugin_speaker_portrait_jobs",
        "claimed_pending_messages",
        False,
    ) in RUNTIME_SCHEMA_COLUMN_CONTRACTS
    assert "idx_speaker_portrait_job_attempt" in RUNTIME_SCHEMA_INDEXES


def test_speaker_portrait_cursor_downgrade_removes_new_contract(monkeypatch) -> None:
    _migration, rendered = _render(monkeypatch, "downgrade")

    assert "DROP INDEX IF EXISTS idx_speaker_portrait_job_attempt" in rendered
    assert "DROP COLUMN IF EXISTS claimed_pending_messages" in rendered
    assert "DROP COLUMN IF EXISTS last_distilled_message_at" in rendered
