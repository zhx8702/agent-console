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

_MODULE = "migrations.versions.20260722_0042_wxbot_report_delivery_ack"


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


def test_report_delivery_ack_upgrade_is_linear_and_quarantines_legacy_rows(
    monkeypatch,
) -> None:
    migration, rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0042_wxbot_report_delivery_ack"
    assert migration.down_revision == "0041_persona_job_queue"
    assert RUNTIME_SCHEMA_COMPATIBILITY_LEVEL == 7
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_heads() == [
        RUNTIME_SCHEMA_REVISION
    ]
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_revision("0043_persona_offline_status").down_revision == (
        migration.revision
    )
    assert "LOCK TABLE plugin_wxbot_report_jobs IN ACCESS EXCLUSIVE MODE" in rendered
    assert "ADD COLUMN sdk_outbound_id BIGINT" in rendered
    assert "ADD COLUMN delivery_queued_at TIMESTAMP WITH TIME ZONE" in rendered
    assert "ADD COLUMN delivery_checked_at TIMESTAMP WITH TIME ZONE" in rendered
    assert "delivery_status IN ('sent', 'sending')" in rendered
    assert "delivery_status = 'indeterminate'" in rendered
    assert "sdk_outbound_id IS NULL" in rendered
    assert "delivered_at = NULL" in rendered
    assert "CREATE INDEX idx_wxbot_report_jobs_queued_delivery" in rendered
    assert "WHERE delivery_status = 'queued'" in rendered
    assert "compatibility_level = 6" in rendered


def test_report_delivery_ack_runtime_contract_covers_columns_and_partial_index() -> None:
    assert (
        "plugin_wxbot_report_jobs",
        "sdk_outbound_id",
        True,
    ) in RUNTIME_SCHEMA_COLUMN_CONTRACTS
    assert (
        "plugin_wxbot_report_jobs",
        "delivery_queued_at",
        True,
    ) in RUNTIME_SCHEMA_COLUMN_CONTRACTS
    assert (
        "plugin_wxbot_report_jobs",
        "delivery_checked_at",
        True,
    ) in RUNTIME_SCHEMA_COLUMN_CONTRACTS
    assert "idx_wxbot_report_jobs_queued_delivery" in RUNTIME_SCHEMA_INDEXES
    assert (
        "idx_wxbot_report_jobs_queued_delivery",
        "plugin_wxbot_report_jobs",
        ("tenant_id", "delivery_checked_at", "delivery_queued_at", "id"),
        "delivery_status='queued'",
    ) in RUNTIME_SCHEMA_INDEX_CONTRACTS


def test_report_delivery_ack_downgrade_quarantines_queued_rows(monkeypatch) -> None:
    _migration, rendered = _render(monkeypatch, "downgrade")

    quarantine = "delivery_status = 'indeterminate'"
    assert quarantine in rendered
    assert rendered.index(quarantine) < rendered.index("DROP COLUMN sdk_outbound_id")
    assert "WHERE delivery_status = 'queued'" in rendered
    assert "compatibility_level = 5" in rendered
