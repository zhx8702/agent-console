from __future__ import annotations

import importlib


def test_turn_tenant_writer_compat_is_single_forward_head(monkeypatch) -> None:
    migration = importlib.import_module(
        "migrations.versions.20260718_0021_turn_tenant_writer_compat"
    )
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda statement: statements.append(str(statement)))

    migration.upgrade()

    sql = "\n".join(statements)
    assert migration.revision == "0021_turn_tenant_writer_compat"
    assert migration.down_revision == "0020_message_effect_intent"
    assert "BEFORE INSERT OR UPDATE ON turns" in sql
    assert "matching_sessions > 1" in sql
    assert "turn tenant/session ownership mismatch" in sql
    assert "NEW.tenant_id := inferred_tenant_id" in sql


def test_turn_tenant_writer_compat_downgrade_only_removes_compatibility_objects(
    monkeypatch,
) -> None:
    migration = importlib.import_module(
        "migrations.versions.20260718_0021_turn_tenant_writer_compat"
    )
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda statement: statements.append(str(statement)))

    migration.downgrade()

    sql = "\n".join(statements).upper()
    assert "DROP TRIGGER IF EXISTS TRG_TURNS_TENANT_WRITER_COMPAT" in sql
    assert "DROP FUNCTION IF EXISTS CS_NORMALIZE_TURN_TENANT_SCOPE" in sql
    assert "DROP TABLE" not in sql
    assert "ALTER TABLE" not in sql
