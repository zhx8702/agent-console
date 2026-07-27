from __future__ import annotations

from pathlib import Path

from app.models import (
    Base,
    SocialScopeControlHistoryRow,
    SocialScopeControlRow,
    SocialTenantMemberControlRow,
    VoiceProfileHistoryRow,
)


def test_social_policy_migration_owns_required_tables_and_memory_audience_columns() -> None:
    source = Path(
        "migrations/versions/20260718_0018_social_policy_contract.py"
    ).read_text(encoding="utf-8")

    for table in (
        "social_group_policy",
        "social_member_policy",
        "social_participation_event",
        "voice_profile",
        "audit_events",
    ):
        assert f'"{table}"' in source
    for column in (
        "audience_scope",
        "origin_session_kind",
        "allowed_session_ids",
        "source_kind",
        "sensitivity_category",
        "expires_at",
    ):
        assert f'"{column}"' in source
    assert 'down_revision = "0017_message_reliability"' in source
    assert "private by default" in source


def test_social_scope_migration_adds_independent_controls_and_voice_history() -> None:
    source = Path(
        "migrations/versions/20260718_0022_social_scope_controls.py"
    ).read_text(encoding="utf-8")
    for table in (
        "social_scope_control",
        "social_scope_control_history",
        "social_tenant_member_control",
        "voice_profile_history",
    ):
        assert f'"{table}"' in source
    assert 'server_default="shadow"' in source
    assert 'down_revision = "0021_turn_tenant_writer_compat"' in source


def test_social_scope_models_are_public_and_registered_in_metadata() -> None:
    expected = {
        "social_scope_control": SocialScopeControlRow,
        "social_scope_control_history": SocialScopeControlHistoryRow,
        "social_tenant_member_control": SocialTenantMemberControlRow,
        "voice_profile_history": VoiceProfileHistoryRow,
    }
    for table_name, model in expected.items():
        assert model.__table__ is Base.metadata.tables[table_name]
