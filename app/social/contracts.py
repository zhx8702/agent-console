from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.social.participation import (
    ParticipationContext,
    ParticipationPolicy,
)

RolloutStage = Literal["shadow", "privacy_5", "style_10", "contextual", "proactive"]
ParticipationControlScope = Literal["global", "tenant"]
ParticipationStatus = Literal["must_reply", "may_reply", "observe_only", "defer", "cancel"]
ParticipationEventKind = Literal["preview", "runtime"]
MemberDeletionState = Literal["none", "requested", "completed", "failed"]
VoiceSampleSource = Literal["manual", "persona", "authorized_group_samples"]
VoiceSampleScope = Literal["none", "current_group"]
MentionSenderStrategy = Literal["never", "reply_or_ambiguous"]


class StrictContract(BaseModel):
    """Base class for public control-plane contracts.

    Rejecting unknown fields is intentional: configuration clients must not
    silently believe that a misspelled safety control was accepted.
    """

    model_config = ConfigDict(extra="forbid")


class KillSwitches(StrictContract):
    """Independent fail-closed switches evaluated from broadest to narrowest."""

    global_enabled: bool = True
    tenant_enabled: bool = True
    group_enabled: bool = True

    @property
    def effective_enabled(self) -> bool:
        return self.global_enabled and self.tenant_enabled and self.group_enabled


class ScopedParticipationControlValues(StrictContract):
    """A real release/tenant control, independent from a group override."""

    enabled: bool = False
    rollout_stage: RolloutStage = "shadow"


class ScopedParticipationControlUpdate(StrictContract):
    control: ScopedParticipationControlValues
    change_reason: Annotated[str, Field(max_length=240)] = ""


class ScopedParticipationControlDocument(StrictContract):
    scope: ParticipationControlScope
    tenant_id: str = ""
    version: Annotated[int, Field(ge=0)] = 0
    control: ScopedParticipationControlValues = Field(
        default_factory=ScopedParticipationControlValues
    )
    updated_by: str = ""
    updated_at: datetime | None = None


class ParticipationPolicyValues(StrictContract):
    threshold: Annotated[int, Field(ge=0, le=200)] = 60
    quiet_start_hour: Annotated[int, Field(ge=0, le=23)] = 23
    quiet_end_hour: Annotated[int, Field(ge=0, le=23)] = 8
    timezone: Annotated[str, Field(min_length=1, max_length=64)] = "Asia/Shanghai"
    max_soft_replies_10m: Annotated[int, Field(ge=0, le=100)] = 2
    max_soft_replies_hour: Annotated[int, Field(ge=0, le=500)] = 6
    max_bot_ratio_last_40: Annotated[float, Field(ge=0, le=1)] = 0.15
    max_consecutive_bot_messages: Annotated[int, Field(ge=0, le=20)] = 2
    proactive_enabled: bool = False
    max_proactive_per_day: Annotated[int, Field(ge=0, le=100)] = 1
    proactive_min_silence_seconds: Annotated[int, Field(ge=0, le=604_800)] = 10_800
    # Outbound mentions are an explicit, versioned group choice.  The default
    # deliberately avoids notifying a member merely because the bot replies.
    mention_sender_strategy: MentionSenderStrategy = "never"
    # This is a prompt-inclusion window, not a physical deletion policy.  Keep
    # it short and bounded so a group cannot accidentally turn its prompt into
    # an unbounded transcript.  Zero disables durable group context entirely.
    prompt_context_retention_seconds: Annotated[int, Field(ge=0, le=86_400)] = 3_600
    rollout_stage: RolloutStage = "contextual"
    rollout_opt_in: bool = False
    proactive_rollout_percent: Annotated[int, Field(ge=0, le=100)] = 5

    def to_domain(self, *, enabled: bool) -> ParticipationPolicy:
        values = self.model_dump(
            exclude={
                "rollout_stage",
                "rollout_opt_in",
                "proactive_rollout_percent",
            }
        )
        return ParticipationPolicy(enabled=enabled, **values)


class VoiceProfile(StrictContract):
    """Versioned, auditable style controls; never a claim of human identity."""

    profile_id: Annotated[str, Field(min_length=1, max_length=64)] = "default"
    version: Annotated[int, Field(ge=0)] = 0
    enabled: bool = False
    sample_source: VoiceSampleSource = "manual"
    sample_scope: VoiceSampleScope = "none"
    authorized_sample_session_ids: list[Annotated[str, Field(min_length=1, max_length=256)]] = (
        Field(default_factory=list, max_length=1)
    )
    authorization_reference: Annotated[str, Field(max_length=240)] = ""
    valid_from: datetime | None = None
    expires_at: datetime | None = None
    display_name: Annotated[str, Field(max_length=128)] = ""
    tone: Annotated[str, Field(min_length=1, max_length=64)] = "natural"
    verbosity: Literal["terse", "concise", "balanced"] = "concise"
    phrase_preferences: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list,
        max_length=30,
    )
    emoji_frequency: Annotated[float, Field(ge=0, le=0.15)] = 0.05
    list_format_policy: Literal["avoid_by_default", "allow"] = "avoid_by_default"
    identity_disclosure: Literal["contextual", "always"] = "contextual"
    source_persona_version: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_persona_source(cls, value: object) -> object:
        if not isinstance(value, dict) or "sample_source" in value:
            return value
        upgraded = dict(value)
        if int(upgraded.get("source_persona_version") or 0) > 0:
            upgraded["sample_source"] = "persona"
        return upgraded

    @field_validator("phrase_preferences", mode="before")
    @classmethod
    def normalize_phrase_preferences(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            return value
        normalized: list[object] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                normalized.append(item)
                continue
            phrase = " ".join(item.split())
            key = unicodedata.normalize("NFKC", phrase).casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(phrase)
        return normalized

    @model_validator(mode="after")
    def validate_source_scope_and_validity(self) -> VoiceProfile:
        session_ids = self.authorized_sample_session_ids
        if len(set(session_ids)) != len(session_ids):
            raise ValueError("authorized sample sessions must be unique")
        if any(session_id != session_id.strip() for session_id in session_ids):
            raise ValueError("authorized sample sessions must be trimmed")
        if self.sample_scope == "none" and session_ids:
            raise ValueError("sample_scope none cannot authorize sample sessions")
        if self.sample_scope == "current_group" and not session_ids:
            raise ValueError("current_group sample scope requires an authorized session")
        if self.sample_source in {"manual", "persona"} and (
            self.sample_scope != "none" or session_ids
        ):
            raise ValueError("manual and persona sources cannot declare group sample scope")
        if self.sample_source == "authorized_group_samples":
            if self.sample_scope != "current_group" or not session_ids:
                raise ValueError("authorized group samples require current_group sample scope")
            if not self.authorization_reference.strip():
                raise ValueError("authorized group samples require an authorization reference")
        if self.sample_source == "persona" and self.source_persona_version < 1:
            raise ValueError("persona source requires source_persona_version")
        for field_name, value in (
            ("valid_from", self.valid_from),
            ("expires_at", self.expires_at),
        ):
            if value is not None and value.utcoffset() is None:
                raise ValueError(f"{field_name} must include a timezone")
        if (
            self.valid_from is not None
            and self.expires_at is not None
            and self.expires_at <= self.valid_from
        ):
            raise ValueError("expires_at must be after valid_from")
        return self

    def sample_authorization_reason(self, session_id: str) -> str:
        authorized_sessions = tuple(self.authorized_sample_session_ids)
        if self.sample_source in {"manual", "persona"} and (
            self.sample_scope != "none" or authorized_sessions
        ):
            return "voice_profile_sample_scope_invalid"
        if self.sample_scope == "current_group":
            if authorized_sessions != (session_id,):
                return "voice_profile_sample_scope_invalid"
        elif authorized_sessions:
            return "voice_profile_sample_scope_invalid"
        if self.sample_source == "authorized_group_samples" and (
            self.sample_scope != "current_group"
            or authorized_sessions != (session_id,)
            or not self.authorization_reference.strip()
        ):
            return "voice_profile_sample_scope_invalid"
        return ""

    def runtime_reason(self, *, session_id: str, now: datetime) -> str:
        if not self.enabled:
            return "voice_profile_disabled"
        authorization_reason = self.sample_authorization_reason(session_id)
        if authorization_reason:
            return authorization_reason
        current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        if self.valid_from is not None and current < self.valid_from:
            return "voice_profile_not_yet_valid"
        if self.expires_at is not None and current >= self.expires_at:
            return "voice_profile_expired"
        return "voice_profile_active"

    def runtime_style_payload(self) -> dict[str, object]:
        """Return only fields needed for style application, not authorization evidence."""

        return self.model_dump(
            mode="json",
            include={
                "profile_id",
                "version",
                "display_name",
                "tone",
                "verbosity",
                "phrase_preferences",
                "emoji_frequency",
                "list_format_policy",
                "identity_disclosure",
                "source_persona_version",
            },
        )


class VoiceProfilePreviewRequest(StrictContract):
    """Transient style preview input; callers must never persist its text fields."""

    voice_profile: VoiceProfile
    reply_text: Annotated[str, Field(min_length=1, max_length=4_000)]
    source_text: Annotated[str, Field(max_length=1_000)] = ""
    explicitly_detailed: bool | None = None


class VoiceProfilePreviewDocument(StrictContract):
    profile_id: str
    version: Annotated[int, Field(ge=0)]
    runtime_reason: str
    applied: bool
    output_text: str
    mode: str
    transformed: bool
    emoji: str = ""
    catchphrase: str = ""
    identity_disclosed: bool = False
    reason_codes: list[str] = Field(default_factory=list, max_length=32)


class GroupParticipationPolicyUpdate(StrictContract):
    kill_switches: KillSwitches | None = None
    policy: ParticipationPolicyValues | None = None
    voice_profile: VoiceProfile | None = None
    rollback_to_version: Annotated[int | None, Field(ge=1)] = None
    change_reason: Annotated[str, Field(max_length=240)] = ""

    @model_validator(mode="after")
    def validate_full_replacement_or_rollback(self) -> GroupParticipationPolicyUpdate:
        if self.rollback_to_version is not None:
            if any(
                value is not None for value in (self.kill_switches, self.policy, self.voice_profile)
            ):
                raise ValueError("rollback_to_version cannot be combined with replacement fields")
            return self
        if self.kill_switches is None or self.policy is None:
            raise ValueError("PUT requires kill_switches and policy")
        return self


class GroupParticipationPolicyDocument(StrictContract):
    tenant_id: str
    session_id: str
    version: Annotated[int, Field(ge=0)]
    kill_switches: KillSwitches
    effective_enabled: bool
    policy: ParticipationPolicyValues
    voice_profile: VoiceProfile | None = None
    updated_by: str = ""
    updated_at: datetime | None = None


class MemberPrivacyValues(StrictContract):
    """Conservative defaults: no durable member memory without an opt-in."""

    memory_enabled: bool = False
    allow_group_recall: bool = False
    allow_private_recall: bool = True
    proactive_participation_enabled: bool = False
    soft_reply_opt_out: bool = False
    no_group_mentions: bool = False
    retention_days: Annotated[int, Field(ge=1, le=3650)] = 30
    audience_scope: Literal["private", "session", "explicit"] = "private"
    allowed_session_ids: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        default_factory=list, max_length=100
    )
    sensitive_memory_enabled: bool = False
    correction_enabled: bool = True
    deletion_enabled: bool = True

    @model_validator(mode="after")
    def validate_audience(self) -> MemberPrivacyValues:
        if self.audience_scope == "explicit" and not self.allowed_session_ids:
            raise ValueError("explicit audience requires allowed_session_ids")
        return self


class MemberPrivacyPolicyUpdate(StrictContract):
    policy: MemberPrivacyValues | None = None
    rollback_to_version: Annotated[int | None, Field(ge=1)] = None
    change_reason: Annotated[str, Field(max_length=240)] = ""

    @model_validator(mode="after")
    def validate_replacement_or_rollback(self) -> MemberPrivacyPolicyUpdate:
        if (self.policy is None) == (self.rollback_to_version is None):
            raise ValueError("provide exactly one of policy or rollback_to_version")
        return self


class MemberPrivacyPolicyDocument(StrictContract):
    tenant_id: str
    session_id: str
    user_id: str
    version: Annotated[int, Field(ge=0)]
    # ``policy`` is the effective fail-closed view consumed by runtime code.
    # Admin clients edit this independent group-local snapshot so a temporary
    # tenant-wide opt-out never gets persisted into the narrower override.
    configured_policy: MemberPrivacyValues | None = None
    policy: MemberPrivacyValues
    updated_by: str = ""
    updated_at: datetime | None = None


class TenantMemberControlValues(StrictContract):
    """Tenant-wide member choices that group overrides may never weaken."""

    memory_opt_out: bool = False
    participation_opt_out: bool = False
    no_group_mentions: bool = False


class TenantMemberControlUpdate(StrictContract):
    control: TenantMemberControlValues
    request_memory_deletion: bool = False
    change_reason: Annotated[str, Field(max_length=240)] = ""


class TenantMemberControlDocument(StrictContract):
    tenant_id: str
    user_id: str
    version: Annotated[int, Field(ge=0)] = 0
    control: TenantMemberControlValues = Field(default_factory=TenantMemberControlValues)
    deletion_state: MemberDeletionState = "none"
    deletion_intent_key: str = ""
    updated_by: str = ""
    updated_at: datetime | None = None


class MemberMemoryForgetEffectPayload(StrictContract):
    """Content-free payload for the durable tenant-member erasure worker."""

    tenant_id: Annotated[str, Field(min_length=1, max_length=64)]
    user_id: Annotated[str, Field(min_length=1, max_length=256)]
    control_version: Annotated[int, Field(ge=1)]
    deletion_intent_key: Annotated[str, Field(min_length=1, max_length=512)]


class PolicyVersionMetadata(StrictContract):
    """Immutable version metadata.  Snapshots and policy text never leave storage."""

    version: Annotated[int, Field(ge=1)]
    parent_version: Annotated[int, Field(ge=0)]
    rollback_from_version: Annotated[int | None, Field(ge=1)] = None
    actor: str = ""
    change_summary: list[str] = Field(default_factory=list, max_length=64)
    reason_present: bool = False
    created_at: datetime


class PolicyVersionPage(StrictContract):
    items: list[PolicyVersionMetadata]
    next_cursor: str | None = None


class MemberMemoryItemDocument(StrictContract):
    """Minimal authorized member-memory view; provenance text is deliberately absent."""

    item_id: int
    content: Annotated[str, Field(max_length=500)]
    memory_type: str
    scope_type: Literal["identity", "session"]
    audience_scope: Literal["private", "session", "explicit"]
    status: str
    sensitivity_category: str
    pinned: bool = False
    expires_at: datetime | None = None
    updated_at: datetime
    etag: str


class MemberMemoryPage(StrictContract):
    items: list[MemberMemoryItemDocument]
    next_cursor: str | None = None


class MemberMemoryCorrection(StrictContract):
    content: Annotated[str, Field(min_length=1, max_length=500)]
    reason: Annotated[str, Field(max_length=240)] = ""


class MemberMemoryDeletionResult(StrictContract):
    item_id: int
    status: Literal["deleted"] = "deleted"
    idempotent_replayed: bool = False


class MemberMemoryCorrectionResolution(StrictContract):
    status: Literal["applied", "not_found", "confirmation_required"]
    changed: Annotated[int, Field(ge=0)] = 0
    candidate_count: Annotated[int, Field(ge=0, le=20)] = 0


class ParticipationPreviewRequest(StrictContract):
    """Structured simulation input that deliberately has no chat-text field."""

    message_id: Annotated[str, Field(min_length=1, max_length=128)] = "preview"
    now: datetime = Field(default_factory=lambda: datetime.now(UTC))
    mentioned_me: bool = False
    replied_to_bot: bool = False
    explicit_command: bool = False
    safety_response_required: bool = False
    explicit_question_to_bot: bool = False
    keyword_triggered: bool = False
    topic_continuation: bool = False
    unfinished_task_continuation: bool = False
    directed_to_other_member: bool = False
    rapid_multi_party_chat: bool = False
    bot_replied_within_60s: bool = False
    valid_member_answer_exists: bool = False
    intent_confidence: Annotated[float, Field(ge=0, le=1)] = 1.0
    base_eligible: bool = False
    base_reason: Literal[
        "",
        "base_policy_not_eligible",
        "not_addressed",
        "channel_suppressed",
        "member_opt_out",
        "group_disabled",
    ] = ""
    bot_messages_last_40: Annotated[int, Field(ge=0, le=40)] = 0
    total_messages_last_40: Annotated[int, Field(ge=0, le=40)] = 0
    soft_replies_last_10m: Annotated[int, Field(ge=0)] = 0
    soft_replies_last_hour: Annotated[int, Field(ge=0)] = 0
    consecutive_bot_messages: Annotated[int, Field(ge=0)] = 0
    proactive_messages_today: Annotated[int, Field(ge=0)] = 0
    group_silence_seconds: Annotated[int, Field(ge=0)] = 0
    is_self_sent: bool = False
    topic_changed: bool = False
    superseded_by_newer_message: bool = False
    requested_proactive: bool = False
    response_kind: Literal["short", "tool_progress", "tool_result"] = "short"
    reply_target_ambiguous: bool = False

    @model_validator(mode="after")
    def validate_message_window(self) -> ParticipationPreviewRequest:
        if self.bot_messages_last_40 > self.total_messages_last_40:
            raise ValueError("bot_messages_last_40 cannot exceed total_messages_last_40")
        return self

    def to_context(self, *, tenant_id: str, session_id: str) -> ParticipationContext:
        return ParticipationContext(
            tenant_id=tenant_id,
            session_id=session_id,
            **self.model_dump(),
        )

    def event_summary(self) -> dict[str, bool | int | float | str]:
        # Identifiers, timestamps, and free-form reasons are not retained in the
        # participation-event table.  This is intentionally limited to the
        # numeric/boolean signals needed to explain a decision.
        excluded = {"message_id", "now", "base_reason"}
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key not in excluded and isinstance(value, (bool, int, float, str))
        }


class ParticipationDecisionDocument(StrictContract):
    event_id: str
    tenant_id: str
    session_id: str
    policy_version: Annotated[int, Field(ge=0)]
    status: ParticipationStatus
    score: int
    reason_codes: list[str]
    not_before: datetime | None = None
    expires_at: datetime | None = None
    mention_sender: bool = False


class ParticipationEventDocument(StrictContract):
    event_id: str
    tenant_id: str
    session_id: str
    policy_version: Annotated[int, Field(ge=0)]
    event_kind: ParticipationEventKind
    runtime_stage: str = "decision"
    delivery_stage: str = "not_applicable"
    status: ParticipationStatus
    score: int
    reason_codes: list[str]
    signal_summary: dict[str, bool | int | float | str]
    trace_id: str = ""
    created_at: datetime


class ParticipationEventPage(StrictContract):
    items: list[ParticipationEventDocument]
    next_before: datetime | None = None
    next_cursor: str | None = None
