from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_message_effect_intent_migration_renders_offline(monkeypatch) -> None:
    migration = import_module(
        "migrations.versions.20260718_0020_message_effect_intent"
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))

    migration.upgrade()

    rendered = output.getvalue()
    assert "CREATE TABLE message_effect_intent" in rendered
    assert "ix_message_effect_intent_due" in rendered
    assert "ix_message_effect_intent_source" in rendered


def test_message_effect_intent_migration_creates_state_machine(monkeypatch) -> None:
    migration = import_module(
        "migrations.versions.20260718_0020_message_effect_intent"
    )
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            monkeypatch.setattr(migration, "op", operations)
            migration.upgrade()

            inspector = inspect(connection)
            columns = {
                str(column["name"])
                for column in inspector.get_columns("message_effect_intent")
            }
            indexes = {
                str(index["name"])
                for index in inspector.get_indexes("message_effect_intent")
            }
            checks = " ".join(
                str(check.get("sqltext") or "").lower()
                for check in inspector.get_check_constraints(
                    "message_effect_intent"
                )
            )
            pk = set(
                inspector.get_pk_constraint("message_effect_intent").get(
                    "constrained_columns"
                )
                or []
            )
    finally:
        engine.dispose()

    assert migration.revision == "0020_message_effect_intent"
    assert migration.down_revision == "0019_group_speech_ledger"
    assert {
        "tenant_id",
        "idempotency_key",
        "source_message_id",
        "session_id",
        "effect_type",
        "payload",
        "context",
        "status",
        "attempts",
        "claim_owner",
        "claim_token",
        "claim_until",
        "available_at",
    } <= columns
    assert pk == {"tenant_id", "idempotency_key"}
    assert {
        "ix_message_effect_intent_due",
        "ix_message_effect_intent_source",
        "ix_message_effect_intent_scope_created",
    } <= indexes
    assert all(status in checks for status in ("prepared", "running", "completed", "failed"))
