from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.migration import MigrationContext
from alembic.operations import Operations


def _render(monkeypatch, operation: str) -> tuple[object, str]:
    migration = import_module(
        "migrations.versions.20260718_0036_wxbot_report_attempt_fencing"
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, operation)()
    return migration, output.getvalue()


def test_wxbot_attempt_fencing_upgrade_and_downgrade(monkeypatch) -> None:
    migration, upgrade = _render(monkeypatch, "upgrade")
    _migration, downgrade = _render(monkeypatch, "downgrade")

    assert migration.down_revision == "0035_plugin_lifecycle_global_index"
    report_lock = (
        "LOCK TABLE plugin_wxbot_report_jobs IN ACCESS EXCLUSIVE MODE"
    )
    self_review_lock = (
        "LOCK TABLE plugin_wxbot_self_review_jobs IN ACCESS EXCLUSIVE MODE"
    )
    assert upgrade.index(report_lock) < upgrade.index(self_review_lock)
    assert upgrade.index(self_review_lock) < upgrade.index("DO $$")
    assert upgrade.index("DO $$") < upgrade.index("ADD COLUMN run_attempt")
    assert "cannot upgrade 0036: drain active wxbot report jobs first" in upgrade
    assert "ADD COLUMN run_attempt BIGINT DEFAULT 0 NOT NULL" in upgrade
    assert "ADD COLUMN delivery_attempt BIGINT DEFAULT 0 NOT NULL" in upgrade
    assert upgrade.count("ADD COLUMN run_attempt BIGINT DEFAULT 0 NOT NULL") == 2
    assert "idx_wxbot_report_jobs_running_lease" in upgrade
    assert "idx_wxbot_report_jobs_sending_lease" in upgrade
    assert "idx_wxbot_self_review_jobs_running_lease" in upgrade
    assert "WHERE status = 'running'" in upgrade
    assert "WHERE delivery_status = 'sending'" in upgrade
    assert "compatibility_level = 4" in upgrade
    assert downgrade.index(report_lock) < downgrade.index(self_review_lock)
    assert downgrade.index(self_review_lock) < downgrade.index("DO $$")
    assert downgrade.index("DO $$") < downgrade.index("DROP COLUMN run_attempt")
    assert "drain active wxbot report jobs first" in downgrade
    assert "compatibility_level = 3" in downgrade
    assert "DROP COLUMN delivery_attempt" in downgrade
