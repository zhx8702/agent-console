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
    RUNTIME_SCHEMA_INDEXES,
    RUNTIME_SCHEMA_REVISION,
    RUNTIME_SCHEMA_TABLES,
)


def _render_upgrade(monkeypatch) -> tuple[object, str]:
    migration = import_module(
        "migrations.versions.20260720_0037_runtime_hardening_merge"
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    migration.upgrade()
    return migration, output.getvalue()


def test_runtime_hardening_histories_merge_after_both_shipped_heads(monkeypatch) -> None:
    migration, upgrade = _render_upgrade(monkeypatch)

    assert migration.revision == "0037_runtime_hardening_merge"
    assert migration.down_revision == (
        "0036_wxbot_report_attempt_fencing",
        "0011_runtime_hardening",
    )
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == [RUNTIME_SCHEMA_REVISION]
    assert RUNTIME_SCHEMA_COMPATIBILITY_LEVEL == 6
    assert "plugin_wxbot_group_membership" in RUNTIME_SCHEMA_TABLES
    assert "idx_wxbot_group_membership_active" in RUNTIME_SCHEMA_INDEXES
    assert (
        "plugin_wxbot_session_policy",
        "reply_cooldown_seconds",
        True,
    ) in RUNTIME_SCHEMA_COLUMN_CONTRACTS
    assert (
        "plugin_wxbot_interaction_cursor",
        "burst_count",
        False,
    ) in RUNTIME_SCHEMA_COLUMN_CONTRACTS
    assert "ADD COLUMN IF NOT EXISTS reply_cooldown_seconds" in upgrade
    assert "INSERT INTO plugin_wxbot_group_membership" in upgrade
    assert "group_observation_context" in upgrade
    assert "historical_group_memory_migration" in upgrade
