from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from app.infra.runtime_schema import (
    RUNTIME_SCHEMA_COLUMN_CONTRACTS,
    RUNTIME_SCHEMA_COMPATIBILITY_LEVEL,
    RUNTIME_SCHEMA_INDEX_CONTRACTS,
    RUNTIME_SCHEMA_INDEXES,
    RUNTIME_SCHEMA_REVISION,
)

_MODULE = "migrations.versions.20260730_0046_memory_event_provenance_expiry"


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


def test_memory_provenance_expiry_upgrade_is_runtime_head(monkeypatch) -> None:
    migration, rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0046_memory_event_provenance_expiry"
    assert migration.down_revision == "0045_wxbot_outbound_files"
    assert RUNTIME_SCHEMA_REVISION == "0049_speaker_portrait_hot_update"
    assert RUNTIME_SCHEMA_COMPATIBILITY_LEVEL == 9
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_heads() == [
        RUNTIME_SCHEMA_REVISION
    ]
    assert "ADD COLUMN source_member_id VARCHAR(128)" in rendered
    assert "ADD COLUMN source_message_id VARCHAR(256)" in rendered
    assert "ADD COLUMN expires_at TIMESTAMP WITH TIME ZONE" in rendered
    assert "ADD COLUMN source_evidence_json JSONB" in rendered
    assert "CREATE INDEX ix_memory_event_member_evidence" in rendered
    assert "CREATE INDEX ix_memory_event_expiry" in rendered
    assert "CREATE INDEX ix_memory_item_expiry_physical" in rendered
    assert "CREATE INDEX ix_memory_item_source_evidence" in rendered
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_event_key" in rendered
    assert "WHERE event_key IS NOT NULL" in rendered
    assert "compatibility_level = 9" in rendered

    for contract in (
        ("plugin_memory_event", "source_member_id", False),
        ("plugin_memory_event", "source_message_id", False),
        ("plugin_memory_event", "expires_at", True),
        ("plugin_memory_item", "source_evidence_json", False),
    ):
        assert contract in RUNTIME_SCHEMA_COLUMN_CONTRACTS
    for index_name in (
        "ix_memory_event_member_evidence",
        "ix_memory_event_source_message",
        "ix_memory_event_expiry",
        "ix_memory_item_expiry_physical",
        "ix_memory_item_source_evidence",
    ):
        assert index_name in RUNTIME_SCHEMA_INDEXES
    assert (
        "ux_memory_event_key",
        "plugin_memory_event",
        ("event_key",),
        "event_key IS NOT NULL",
    ) in RUNTIME_SCHEMA_INDEX_CONTRACTS


def test_memory_provenance_expiry_downgrade_restores_compatibility(monkeypatch) -> None:
    _migration, rendered = _render(monkeypatch, "downgrade")

    assert "compatibility_level = 8" in rendered
    assert "DROP COLUMN source_evidence_json" in rendered
    assert "DROP COLUMN source_message_id" in rendered
    assert "DROP COLUMN source_member_id" in rendered
