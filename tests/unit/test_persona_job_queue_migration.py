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

_MODULE = "migrations.versions.20260721_0041_persona_job_queue"


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


def test_persona_job_queue_upgrade_is_linear_and_fenced(monkeypatch) -> None:
    migration, rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0041_persona_job_queue"
    assert migration.down_revision == "0039_channel_connection_activity"
    assert RUNTIME_SCHEMA_REVISION == "0043_persona_offline_status"
    assert RUNTIME_SCHEMA_COMPATIBILITY_LEVEL == 6
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_heads() == [
        RUNTIME_SCHEMA_REVISION
    ]
    assert (
        ScriptDirectory.from_config(Config("alembic.ini"))
        .get_revision("0042_wxbot_report_delivery_ack")
        .down_revision
        == migration.revision
    )
    assert "LOCK TABLE plugin_persona_jobs IN ACCESS EXCLUSIVE MODE" in rendered
    assert "drain active persona jobs first" in rendered
    assert "CREATE TABLE plugin_persona_job_chunks" in rendered
    assert "ADD CONSTRAINT uq_persona_jobs_tenant_request UNIQUE" in rendered
    assert "CREATE INDEX idx_persona_jobs_ready" in rendered
    assert "CREATE INDEX idx_persona_jobs_running_lease" in rendered
    assert "compatibility_level = 5" in rendered


def test_persona_job_queue_downgrade_requires_drained_queue(monkeypatch) -> None:
    _migration, rendered = _render(monkeypatch, "downgrade")

    assert "finish or cancel persona jobs first" in rendered
    assert "DROP TABLE plugin_persona_job_chunks" in rendered
    assert "compatibility_level = 4" in rendered
