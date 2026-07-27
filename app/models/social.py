from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return str(uuid4())


class SocialScopeControlRow(Base):
    __tablename__ = "social_scope_control"
    __table_args__ = (
        CheckConstraint(
            "scope_kind IN ('global', 'tenant')",
            name="ck_social_scope_control_kind",
        ),
        CheckConstraint("version > 0", name="ck_social_scope_control_version"),
    )

    scope_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rollout_stage: Mapped[str] = mapped_column(String(32), nullable=False, default="shadow")
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class SocialScopeControlHistoryRow(Base):
    __tablename__ = "social_scope_control_history"
    __table_args__ = (
        UniqueConstraint(
            "scope_kind",
            "tenant_id",
            "version",
            name="uq_social_scope_control_history_version",
        ),
        Index(
            "ix_social_scope_control_history_scope_created",
            "scope_kind",
            "tenant_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    change_reason: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SocialTenantMemberControlRow(Base):
    __tablename__ = "social_tenant_member_control"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_social_tenant_member_control_version"),
        CheckConstraint(
            "deletion_state IN ('none', 'requested', 'completed', 'failed')",
            name="ck_social_tenant_member_control_deletion_state",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_opt_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    participation_opt_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    no_group_mentions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deletion_state: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    deletion_intent_key: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class SocialGroupPolicyRow(Base):
    __tablename__ = "social_group_policy"
    __table_args__ = (CheckConstraint("version > 0", name="ck_social_group_policy_version"),)

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    global_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tenant_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # New groups inherit the enabled baseline.  Persisting ``False`` is the
    # explicit per-group opt-out; global and tenant release gates remain
    # independent controls.
    group_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    voice_profile_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class SocialGroupPolicyHistoryRow(Base):
    __tablename__ = "social_group_policy_history"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "session_id",
            "version",
            name="uq_social_group_policy_history_version",
        ),
        Index(
            "ix_social_group_policy_history_scope_created",
            "tenant_id",
            "session_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(256), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rollback_from_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    change_reason: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SocialMemberPolicyRow(Base):
    __tablename__ = "social_member_policy"
    __table_args__ = (CheckConstraint("version > 0", name="ck_social_member_policy_version"),)

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class SocialMemberPolicyHistoryRow(Base):
    __tablename__ = "social_member_policy_history"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "session_id",
            "user_id",
            "version",
            name="uq_social_member_policy_history_version",
        ),
        Index(
            "ix_social_member_policy_history_scope_created",
            "tenant_id",
            "session_id",
            "user_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(256), nullable=False)
    user_id: Mapped[str] = mapped_column(String(256), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rollback_from_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    change_reason: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SocialParticipationEventRow(Base):
    __tablename__ = "social_participation_event"
    __table_args__ = (
        CheckConstraint(
            "event_kind IN ('preview', 'runtime')",
            name="ck_social_participation_event_kind",
        ),
        CheckConstraint(
            "status IN ('must_reply', 'may_reply', 'observe_only', 'defer', 'cancel')",
            name="ck_social_participation_event_status",
        ),
        Index(
            "ix_social_participation_event_scope_created",
            "tenant_id",
            "session_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(256), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    runtime_stage: Mapped[str] = mapped_column(String(32), nullable=False, default="decision")
    delivery_stage: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_applicable"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_codes_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    signal_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class VoiceProfileRow(Base):
    __tablename__ = "voice_profile"
    __table_args__ = (
        CheckConstraint("version >= 0", name="ck_voice_profile_version"),
        Index(
            "ix_voice_profile_scope_updated",
            "tenant_id",
            "session_id",
            "updated_at",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class VoiceProfileHistoryRow(Base):
    __tablename__ = "voice_profile_history"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "session_id",
            "version",
            name="uq_voice_profile_history_scope_version",
        ),
        Index(
            "ix_voice_profile_history_scope_created",
            "tenant_id",
            "session_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(256), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rollback_from_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    change_reason: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_scope_created", "tenant_id", "session_id", "created_at"),
        Index("ix_audit_events_trace", "trace_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    user_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    before_state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    after_state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SocialPolicyIdempotencyRow(Base):
    __tablename__ = "social_policy_idempotency"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "resource_kind",
            "resource_key",
            "idempotency_key",
            name="uq_social_policy_idempotency_scope_key",
        ),
        Index("ix_social_policy_idempotency_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_key: Mapped[str] = mapped_column(String(640), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
