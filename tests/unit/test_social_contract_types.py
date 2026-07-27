from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.admin.capability_registry import (
    Capability,
    CapabilityHealth,
    CapabilityRegistry,
)
from app.social.contracts import (
    MemberPrivacyValues,
    ParticipationPreviewRequest,
    VoiceProfile,
)


def test_capability_registry_is_typed_duplicate_safe_and_permission_aware() -> None:
    registry = CapabilityRegistry(
        [
            Capability(
                id="social.participation",
                enabled=True,
                available=True,
                health=CapabilityHealth.READY,
                required_permissions=("admin:write",),
            )
        ]
    )

    assert registry.available("social.participation", permissions=("admin:write",))
    assert not registry.available("social.participation", permissions=("admin:read",))
    assert registry.snapshot()[0].health is CapabilityHealth.READY
    with pytest.raises(ValueError, match="duplicate capability"):
        CapabilityRegistry([registry["social.participation"], registry["social.participation"]])


def test_voice_and_privacy_contracts_reject_unsafe_or_ambiguous_values() -> None:
    voice = VoiceProfile(identity_disclosure="contextual", emoji_frequency=0.1)
    assert voice.list_format_policy == "avoid_by_default"
    assert voice.enabled is False
    assert voice.sample_source == "manual"

    with pytest.raises(ValidationError):
        VoiceProfile(identity_disclosure="never")
    with pytest.raises(ValidationError):
        VoiceProfile(chat_text="not accepted")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        MemberPrivacyValues(audience_scope="explicit", allowed_session_ids=[])
    with pytest.raises(ValidationError):
        ParticipationPreviewRequest(chat_text="not accepted")
    with pytest.raises(ValidationError):
        ParticipationPreviewRequest(base_reason="arbitrary chat content")


def test_voice_profile_governance_rejects_private_cross_audience_and_bad_validity() -> None:
    now = datetime.now(UTC)
    valid = VoiceProfile(
        enabled=True,
        sample_source="authorized_group_samples",
        sample_scope="current_group",
        authorized_sample_session_ids=["room@chatroom"],
        authorization_reference="approval-42",
        valid_from=now,
        expires_at=now + timedelta(days=30),
    )
    assert valid.runtime_reason(
        session_id="room@chatroom",
        now=now + timedelta(seconds=1),
    ) == "voice_profile_active"
    assert valid.runtime_reason(
        session_id="other@chatroom",
        now=now + timedelta(seconds=1),
    ) == "voice_profile_sample_scope_invalid"
    runtime_payload = valid.runtime_style_payload()
    assert "authorized_sample_session_ids" not in runtime_payload
    assert "authorization_reference" not in runtime_payload
    assert "valid_from" not in runtime_payload
    assert "expires_at" not in runtime_payload

    with pytest.raises(ValidationError):
        VoiceProfile(sample_scope="private")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        VoiceProfile(
            sample_source="manual",
            sample_scope="current_group",
            authorized_sample_session_ids=["room@chatroom"],
        )
    with pytest.raises(ValidationError):
        VoiceProfile(
            sample_source="authorized_group_samples",
            sample_scope="current_group",
            authorized_sample_session_ids=["room@chatroom"],
        )
    with pytest.raises(ValidationError):
        VoiceProfile(sample_source="persona", source_persona_version=0)
    with pytest.raises(ValidationError):
        VoiceProfile(valid_from=datetime(2026, 7, 18, 8, 0))
    with pytest.raises(ValidationError):
        VoiceProfile(valid_from=now, expires_at=now - timedelta(seconds=1))
