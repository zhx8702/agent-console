from __future__ import annotations

from importlib import import_module
from io import StringIO

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from app.infra.runtime_schema import (
    RUNTIME_SCHEMA_COMPATIBILITY_LEVEL,
    RUNTIME_SCHEMA_INDEX_CONTRACTS,
    RUNTIME_SCHEMA_INDEXES,
    RUNTIME_SCHEMA_REVISION,
)

_MODULE = "migrations.versions.20260728_0044_persona_profile_catalog"


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


def test_persona_profile_catalog_upgrade_restores_saved_skill_catalog(
    monkeypatch,
) -> None:
    migration, rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0044_persona_profile_catalog"
    assert migration.down_revision == "0043_persona_offline_status"
    assert RUNTIME_SCHEMA_COMPATIBILITY_LEVEL == 9
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_heads() == [
        RUNTIME_SCHEMA_REVISION
    ]
    assert (
        ScriptDirectory.from_config(Config("alembic.ini"))
        .get_revision("0045_wxbot_outbound_files")
        .down_revision
        == migration.revision
    )
    assert (
        ScriptDirectory.from_config(Config("alembic.ini"))
        .get_revision(RUNTIME_SCHEMA_REVISION)
        .down_revision
        == "0049_speaker_portrait_hot_update"
    )
    assert "LOCK TABLE plugin_persona_profiles IN ACCESS EXCLUSIVE MODE" in rendered
    assert (
        "DROP CONSTRAINT "
        "plugin_persona_profiles_tenant_id_session_id_channel_source_key"
    ) in rendered
    assert "CREATE UNIQUE INDEX ux_persona_profiles_active_scope" in rendered
    assert "WHERE enabled" in rendered
    assert "CREATE UNIQUE INDEX ux_persona_profiles_scope_skill" in rendered
    assert "WHERE skill_slug <> ''" in rendered
    assert "compatibility_level = 7" in rendered


def test_persona_profile_catalog_runtime_contract_covers_partial_indexes() -> None:
    assert "ux_persona_profiles_active_scope" in RUNTIME_SCHEMA_INDEXES
    assert "ux_persona_profiles_scope_skill" in RUNTIME_SCHEMA_INDEXES
    assert (
        "ux_persona_profiles_active_scope",
        "plugin_persona_profiles",
        ("tenant_id", "session_id", "channel", "source_key"),
        "enabled",
    ) in RUNTIME_SCHEMA_INDEX_CONTRACTS
    assert (
        "ux_persona_profiles_scope_skill",
        "plugin_persona_profiles",
        ("tenant_id", "session_id", "channel", "source_key", "skill_slug"),
        "skill_slug<>''",
    ) in RUNTIME_SCHEMA_INDEX_CONTRACTS


def test_persona_profile_catalog_downgrade_refuses_multiple_saved_skills(
    monkeypatch,
) -> None:
    _migration, rendered = _render(monkeypatch, "downgrade")

    assert "HAVING COUNT(*) > 1" in rendered
    assert "remove extra saved persona skills first" in rendered
    assert rendered.index("RAISE EXCEPTION") < rendered.index(
        "DROP INDEX ux_persona_profiles_scope_skill"
    )
    assert "compatibility_level = 6" in rendered
