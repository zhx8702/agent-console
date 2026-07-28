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

_MODULE = "migrations.versions.20260728_0043_persona_offline_status"


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


def test_persona_offline_status_upgrade_extends_the_single_head(monkeypatch) -> None:
    migration, rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0043_persona_offline_status"
    assert migration.down_revision == "0042_wxbot_report_delivery_ack"
    assert RUNTIME_SCHEMA_COMPATIBILITY_LEVEL == 7
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_heads() == [
        RUNTIME_SCHEMA_REVISION
    ]
    assert (
        ScriptDirectory.from_config(Config("alembic.ini"))
        .get_revision(RUNTIME_SCHEMA_REVISION)
        .down_revision
        == migration.revision
    )
    assert "LOCK TABLE plugin_persona_jobs IN ACCESS EXCLUSIVE MODE" in rendered
    assert "DROP CONSTRAINT ck_persona_jobs_status" in rendered
    assert "'awaiting_import'" in rendered


def test_persona_offline_status_downgrade_requires_completed_imports(
    monkeypatch,
) -> None:
    _migration, rendered = _render(monkeypatch, "downgrade")

    assert "WHERE status = 'awaiting_import'" in rendered
    assert "import or cancel offline persona jobs first" in rendered
    assert rendered.index("RAISE EXCEPTION") < rendered.index(
        "DROP CONSTRAINT ck_persona_jobs_status"
    )
