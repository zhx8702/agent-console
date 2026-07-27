from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_wechat_sdk_token_references_are_removed(monkeypatch) -> None:
    migration = import_module(
        "migrations.versions.20260720_0038_wechat_sdk_no_token"
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))

    migration.upgrade()
    rendered = output.getvalue()

    assert migration.revision == "0038_wechat_sdk_no_token"
    assert migration.down_revision == "0037_runtime_hardening_merge"
    assert "UPDATE channel_connection" in rendered
    assert "adapter_id = 'wechat-sdk'" in rendered
    assert "secret_status = 'not_required'" in rendered
    assert "WHEN desired_state = 'enabled' THEN 'unverified'" in rendered
