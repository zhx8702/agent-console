from __future__ import annotations

import hashlib
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import String, and_, desc, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin.authorization import Principal
from app.models.reliability import MessageEffectIntentRow
from app.models.social import (
    AuditEventRow,
    SocialGroupPolicyHistoryRow,
    SocialGroupPolicyRow,
    SocialMemberPolicyHistoryRow,
    SocialMemberPolicyRow,
    SocialParticipationEventRow,
    SocialPolicyIdempotencyRow,
    SocialScopeControlHistoryRow,
    SocialScopeControlRow,
    SocialTenantMemberControlRow,
    VoiceProfileHistoryRow,
    VoiceProfileRow,
)
from app.social.contracts import (
    GroupParticipationPolicyDocument,
    GroupParticipationPolicyUpdate,
    KillSwitches,
    MemberDeletionState,
    MemberMemoryForgetEffectPayload,
    MemberPrivacyPolicyDocument,
    MemberPrivacyPolicyUpdate,
    MemberPrivacyValues,
    ParticipationControlScope,
    ParticipationEventDocument,
    ParticipationEventKind,
    ParticipationEventPage,
    ParticipationPolicyValues,
    ParticipationStatus,
    PolicyVersionMetadata,
    PolicyVersionPage,
    RolloutStage,
    ScopedParticipationControlDocument,
    ScopedParticipationControlUpdate,
    ScopedParticipationControlValues,
    TenantMemberControlDocument,
    TenantMemberControlUpdate,
    TenantMemberControlValues,
    VoiceProfile,
)
from app.social.participation import ParticipationDecision


class SocialPolicyStoreError(RuntimeError):
    pass


class VersionConflictError(SocialPolicyStoreError):
    def __init__(self, *, expected: int, current: int) -> None:
        super().__init__(f"expected version {expected}, current version {current}")
        self.expected = expected
        self.current = current


class IdempotencyConflictError(SocialPolicyStoreError):
    pass


class HistoryVersionNotFoundError(SocialPolicyStoreError):
    def __init__(self, version: int) -> None:
        super().__init__(f"history version {version} not found")
        self.version = version


class VoiceProfileScopeError(SocialPolicyStoreError):
    """The declared sample authorization does not match the policy group."""

    code = "voice_profile_sample_scope_invalid"


@dataclass(frozen=True, slots=True)
class MutationResult:
    document: (
        GroupParticipationPolicyDocument
        | MemberPrivacyPolicyDocument
        | ScopedParticipationControlDocument
        | TenantMemberControlDocument
    )
    replayed: bool = False


class SocialPolicyStore:
    """Transactional SQLAlchemy store for group behavior and member privacy.

    Every successful mutation updates the current row, appends a full history
    snapshot, writes a structured audit event, and records its idempotent result
    in the same database transaction.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_release_control(self) -> ScopedParticipationControlDocument:
        return await self._get_scope_control("global", "")

    async def get_tenant_control(self, tenant_id: str) -> ScopedParticipationControlDocument:
        return await self._get_scope_control("tenant", tenant_id)

    async def _get_scope_control(
        self,
        scope: ParticipationControlScope,
        tenant_id: str,
    ) -> ScopedParticipationControlDocument:
        async with self._session_factory() as db:
            row = await db.get(
                SocialScopeControlRow,
                {"scope_kind": scope, "tenant_id": tenant_id},
            )
            return _scope_control_document(scope, tenant_id, row)

    async def put_release_control(
        self,
        *,
        expected_version: int,
        update: ScopedParticipationControlUpdate,
        principal: Principal,
        idempotency_key: str = "",
        trace_id: str = "",
    ) -> MutationResult:
        return await self._put_scope_control(
            scope="global",
            tenant_id="",
            expected_version=expected_version,
            update=update,
            principal=principal,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    async def put_tenant_control(
        self,
        *,
        tenant_id: str,
        expected_version: int,
        update: ScopedParticipationControlUpdate,
        principal: Principal,
        idempotency_key: str = "",
        trace_id: str = "",
    ) -> MutationResult:
        return await self._put_scope_control(
            scope="tenant",
            tenant_id=tenant_id,
            expected_version=expected_version,
            update=update,
            principal=principal,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    async def _put_scope_control(
        self,
        *,
        scope: ParticipationControlScope,
        tenant_id: str,
        expected_version: int,
        update: ScopedParticipationControlUpdate,
        principal: Principal,
        idempotency_key: str,
        trace_id: str,
    ) -> MutationResult:
        idempotency_tenant = tenant_id or "__platform__"
        resource_kind = f"{scope}_control"
        request_hash = _request_hash(expected_version, update.model_dump(mode="json"))
        async with self._session_factory() as db:
            async with db.begin():
                replay = await self._idempotent_replay(
                    db,
                    tenant_id=idempotency_tenant,
                    resource_kind=resource_kind,
                    resource_key=_resource_key(scope, tenant_id),
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    document_type=ScopedParticipationControlDocument,
                )
                if replay is not None:
                    return MutationResult(document=replay, replayed=True)
                row = await db.scalar(
                    select(SocialScopeControlRow)
                    .where(
                        SocialScopeControlRow.scope_kind == scope,
                        SocialScopeControlRow.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
                before = _scope_control_document(scope, tenant_id, row)
                if before.version != expected_version:
                    raise VersionConflictError(
                        expected=expected_version,
                        current=before.version,
                    )
                now = datetime.now(UTC)
                if row is None:
                    row = SocialScopeControlRow(
                        scope_kind=scope,
                        tenant_id=tenant_id,
                        version=1,
                    )
                    db.add(row)
                row.version = before.version + 1
                row.enabled = update.control.enabled
                row.rollout_stage = update.control.rollout_stage
                row.updated_by = principal.subject
                row.updated_at = now
                after = _scope_control_document(scope, tenant_id, row)
                after_json = after.model_dump(mode="json")
                db.add(
                    SocialScopeControlHistoryRow(
                        scope_kind=scope,
                        tenant_id=tenant_id,
                        version=after.version,
                        parent_version=before.version,
                        snapshot_json=after_json,
                        actor=principal.subject,
                        change_reason=update.change_reason,
                    )
                )
                db.add(
                    _audit_row(
                        tenant_id=idempotency_tenant,
                        session_id="",
                        user_id="",
                        principal=principal,
                        action=f"social_{scope}_control.update",
                        target_type="social_scope_control",
                        before=before.model_dump(mode="json"),
                        after=after_json,
                        version=after.version,
                        trace_id=trace_id,
                        idempotency_key=idempotency_key,
                        reason=update.change_reason,
                    )
                )
                self._record_idempotency(
                    db,
                    tenant_id=idempotency_tenant,
                    resource_kind=resource_kind,
                    resource_key=_resource_key(scope, tenant_id),
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    response=after_json,
                )
            return MutationResult(document=after)

    async def get_tenant_member_control(
        self,
        tenant_id: str,
        user_id: str,
    ) -> TenantMemberControlDocument:
        async with self._session_factory() as db:
            row = await db.get(
                SocialTenantMemberControlRow,
                {"tenant_id": tenant_id, "user_id": user_id},
            )
            return _tenant_member_document(tenant_id, user_id, row)

    async def put_tenant_member_control(
        self,
        *,
        tenant_id: str,
        user_id: str,
        expected_version: int,
        update: TenantMemberControlUpdate,
        principal: Principal,
        idempotency_key: str,
        trace_id: str = "",
    ) -> MutationResult:
        resource_key = _resource_key(user_id)
        request_hash = _request_hash(expected_version, update.model_dump(mode="json"))
        async with self._session_factory() as db:
            async with db.begin():
                replay = await self._idempotent_replay(
                    db,
                    tenant_id=tenant_id,
                    resource_kind="tenant_member_control",
                    resource_key=resource_key,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    document_type=TenantMemberControlDocument,
                )
                if replay is not None:
                    return MutationResult(document=replay, replayed=True)
                row = await db.scalar(
                    select(SocialTenantMemberControlRow)
                    .where(
                        SocialTenantMemberControlRow.tenant_id == tenant_id,
                        SocialTenantMemberControlRow.user_id == user_id,
                    )
                    .with_for_update()
                )
                before = _tenant_member_document(tenant_id, user_id, row)
                if before.version != expected_version:
                    raise VersionConflictError(
                        expected=expected_version,
                        current=before.version,
                    )
                now = datetime.now(UTC)
                if row is None:
                    row = SocialTenantMemberControlRow(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        version=1,
                        memory_opt_out=False,
                        participation_opt_out=False,
                        no_group_mentions=False,
                        deletion_state="none",
                        deletion_intent_key="",
                        updated_by="",
                    )
                    db.add(row)
                row.version = before.version + 1
                deletion_in_flight = before.deletion_state in {"requested", "failed"}
                row.memory_opt_out = bool(
                    update.request_memory_deletion
                    or deletion_in_flight
                    or update.control.memory_opt_out
                )
                row.participation_opt_out = update.control.participation_opt_out
                row.no_group_mentions = update.control.no_group_mentions
                row.updated_by = principal.subject
                row.updated_at = now
                if update.request_memory_deletion:
                    intent_key = f"member-memory-delete:{idempotency_key}"[:512]
                    row.deletion_state = "requested"
                    row.deletion_intent_key = intent_key
                    synthetic_session = _member_control_session_id(tenant_id, user_id)
                    effect_payload = MemberMemoryForgetEffectPayload(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        control_version=row.version,
                        deletion_intent_key=intent_key,
                    ).model_dump(mode="json")
                    existing_intent = await db.get(
                        MessageEffectIntentRow,
                        {"tenant_id": tenant_id, "idempotency_key": intent_key},
                    )
                    if existing_intent is None:
                        db.add(
                            MessageEffectIntentRow(
                                tenant_id=tenant_id,
                                idempotency_key=intent_key,
                                source_message_id=f"admin:{idempotency_key}"[:128],
                                session_id=synthetic_session,
                                trace_id=trace_id[:64],
                                # Privacy erasure is a kernel compensation,
                                # not ordinary memory-plugin execution.  It
                                # must keep draining while memory is disabled.
                                owner="core",
                                producer_owner="core",
                                effect_type="forget_member",
                                payload=effect_payload,
                                context={
                                    "source": "tenant_member_control",
                                    "event": {
                                        "message_id": f"admin:{idempotency_key}"[:128],
                                        "tenant_id": tenant_id,
                                        "channel": "wechat",
                                        "user_id": user_id,
                                        "session_id": synthetic_session,
                                        "message": {"type": "event", "content": ""},
                                        "trace_id": trace_id[:64],
                                        "metadata": {
                                            "effect_intent_source": "tenant_member_control"
                                        },
                                    },
                                },
                                status="prepared",
                            )
                        )
                after = _tenant_member_document(tenant_id, user_id, row)
                after_json = after.model_dump(mode="json")
                db.add(
                    _audit_row(
                        tenant_id=tenant_id,
                        session_id="",
                        user_id=user_id,
                        principal=principal,
                        action="tenant_member_control.update",
                        target_type="social_tenant_member_control",
                        before=before.model_dump(mode="json"),
                        after=after_json,
                        version=after.version,
                        trace_id=trace_id,
                        idempotency_key=idempotency_key,
                        reason=update.change_reason,
                    )
                )
                self._record_idempotency(
                    db,
                    tenant_id=tenant_id,
                    resource_kind="tenant_member_control",
                    resource_key=resource_key,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    response=after_json,
                )
            return MutationResult(document=after)

    async def mark_tenant_member_deletion_result(
        self,
        *,
        tenant_id: str,
        user_id: str,
        deletion_intent_key: str,
        succeeded: bool,
        trace_id: str = "",
    ) -> bool:
        """Finalize only the still-current erasure request.

        A reclaimed stale intent may safely erase data, but it must never
        overwrite the status of a newer request for the same member.
        """

        async with self._session_factory() as db:
            async with db.begin():
                row = await db.scalar(
                    select(SocialTenantMemberControlRow)
                    .where(
                        SocialTenantMemberControlRow.tenant_id == tenant_id,
                        SocialTenantMemberControlRow.user_id == user_id,
                    )
                    .with_for_update()
                )
                if row is None or row.deletion_intent_key != deletion_intent_key:
                    return False
                before_state = row.deletion_state
                after_state = "completed" if succeeded else "failed"
                row.deletion_state = after_state
                # Erasure requests remain fail closed even when a physical
                # deletion attempt must be retried.
                row.memory_opt_out = True
                row.updated_at = datetime.now(UTC)
                if before_state != after_state:
                    # Completion/failure changes the GET representation and
                    # must advance the ETag so stale admin writes conflict.
                    row.version += 1
                    row.updated_by = "effect-intent-relay"
                    db.add(
                        AuditEventRow(
                            tenant_id=tenant_id,
                            session_id="",
                            user_id=user_id,
                            actor="effect-intent-relay",
                            actor_kind="service_account",
                            action=f"tenant_member_memory_deletion.{after_state}",
                            target_type="social_tenant_member_control",
                            before_state_json={"deletion_state": before_state},
                            after_state_json={"deletion_state": after_state},
                            policy_version=row.version,
                            trace_id=str(trace_id or "")[:128],
                            idempotency_key=deletion_intent_key[:128],
                            reason="durable_member_memory_erasure",
                        )
                    )
                return True

    async def get_group_policy(
        self,
        tenant_id: str,
        session_id: str,
    ) -> GroupParticipationPolicyDocument:
        async with self._session_factory() as db:
            row = await db.get(
                SocialGroupPolicyRow,
                {"tenant_id": tenant_id, "session_id": session_id},
            )
            return await self._group_document(db, tenant_id, session_id, row)

    async def put_group_policy(
        self,
        *,
        tenant_id: str,
        session_id: str,
        expected_version: int,
        update: GroupParticipationPolicyUpdate,
        principal: Principal,
        idempotency_key: str = "",
        trace_id: str = "",
    ) -> MutationResult:
        resource_key = _resource_key(session_id)
        request_hash = _request_hash(expected_version, update.model_dump(mode="json"))
        async with self._session_factory() as db:
            async with db.begin():
                replay = await self._idempotent_replay(
                    db,
                    tenant_id=tenant_id,
                    resource_kind="group_policy",
                    resource_key=resource_key,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    document_type=GroupParticipationPolicyDocument,
                )
                if replay is not None:
                    return MutationResult(document=replay, replayed=True)

                row = await db.scalar(
                    select(SocialGroupPolicyRow)
                    .where(
                        SocialGroupPolicyRow.tenant_id == tenant_id,
                        SocialGroupPolicyRow.session_id == session_id,
                    )
                    .with_for_update()
                )
                before = await self._group_document(db, tenant_id, session_id, row)
                if expected_version != before.version:
                    raise VersionConflictError(
                        expected=expected_version,
                        current=before.version,
                    )

                rollback_from: int | None = None
                if update.rollback_to_version is not None:
                    rollback_from = update.rollback_to_version
                    history = await db.scalar(
                        select(SocialGroupPolicyHistoryRow).where(
                            SocialGroupPolicyHistoryRow.tenant_id == tenant_id,
                            SocialGroupPolicyHistoryRow.session_id == session_id,
                            SocialGroupPolicyHistoryRow.version == rollback_from,
                        )
                    )
                    if history is None:
                        raise HistoryVersionNotFoundError(rollback_from)
                    historical = GroupParticipationPolicyDocument.model_validate(
                        history.snapshot_json
                    )
                    switches = await self._effective_switches(
                        db,
                        tenant_id=tenant_id,
                        group_enabled=historical.kill_switches.group_enabled,
                    )
                    policy = historical.policy
                    voice_profile = historical.voice_profile
                else:
                    assert update.kill_switches is not None
                    assert update.policy is not None
                    # A group mutation owns only the group override.  Global
                    # release and tenant controls are independent resources.
                    switches = await self._effective_switches(
                        db,
                        tenant_id=tenant_id,
                        group_enabled=update.kill_switches.group_enabled,
                    )
                    policy = update.policy
                    voice_profile = update.voice_profile

                if voice_profile is not None:
                    _require_voice_profile_scope(voice_profile, session_id=session_id)

                now = datetime.now(UTC)
                new_version = before.version + 1
                versioned_voice = (
                    voice_profile.model_copy(update={"version": new_version})
                    if voice_profile is not None
                    else None
                )
                if row is None:
                    row = SocialGroupPolicyRow(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        version=new_version,
                    )
                    db.add(row)
                row.version = new_version
                # Retained legacy columns mirror the effective controls for
                # rollback compatibility, but are never authoritative reads.
                row.global_enabled = switches.global_enabled
                row.tenant_enabled = switches.tenant_enabled
                row.group_enabled = switches.group_enabled
                row.policy_json = policy.model_dump(mode="json")
                row.voice_profile_id = (
                    versioned_voice.profile_id if versioned_voice is not None else ""
                )
                row.updated_by = principal.subject
                row.updated_at = now

                if versioned_voice is not None:
                    await self._upsert_voice_profile(
                        db,
                        tenant_id=tenant_id,
                        session_id=session_id,
                        profile=versioned_voice,
                        parent_version=before.version,
                        rollback_from_version=rollback_from,
                        actor=principal.subject,
                        change_reason=update.change_reason,
                        now=now,
                    )
                elif before.voice_profile is not None:
                    # A removal is itself a versioned VoiceProfile state.  Keep
                    # the tombstone in the immutable stream so operators can
                    # see and roll back a profile disable without relying on
                    # the broader group-policy history.
                    db.add(
                        VoiceProfileHistoryRow(
                            tenant_id=tenant_id,
                            session_id=session_id,
                            profile_id=before.voice_profile.profile_id,
                            version=new_version,
                            parent_version=before.version,
                            rollback_from_version=rollback_from,
                            snapshot_json={
                                "removed": True,
                                "profile_id": before.voice_profile.profile_id,
                            },
                            actor=principal.subject,
                            change_reason=update.change_reason,
                            created_at=now,
                        )
                    )

                after = GroupParticipationPolicyDocument(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    version=new_version,
                    kill_switches=switches,
                    effective_enabled=switches.effective_enabled,
                    policy=policy.model_copy(
                        update={"rollout_stage": await self._effective_rollout_stage(db, tenant_id)}
                    ),
                    voice_profile=versioned_voice,
                    updated_by=principal.subject,
                    updated_at=now,
                )
                after_json = after.model_dump(mode="json")
                db.add(
                    SocialGroupPolicyHistoryRow(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        version=new_version,
                        parent_version=before.version,
                        rollback_from_version=rollback_from,
                        snapshot_json=after_json,
                        actor=principal.subject,
                        change_reason=update.change_reason,
                    )
                )
                db.add(
                    _audit_row(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        user_id="",
                        principal=principal,
                        action=(
                            "participation_policy.rollback"
                            if rollback_from is not None
                            else "participation_policy.update"
                        ),
                        target_type="social_group_policy",
                        before=before.model_dump(mode="json"),
                        after=after_json,
                        version=new_version,
                        trace_id=trace_id,
                        idempotency_key=idempotency_key,
                        reason=update.change_reason,
                    )
                )
                self._record_idempotency(
                    db,
                    tenant_id=tenant_id,
                    resource_kind="group_policy",
                    resource_key=resource_key,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    response=after_json,
                )
            return MutationResult(document=after)

    async def get_member_policy(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
    ) -> MemberPrivacyPolicyDocument:
        async with self._session_factory() as db:
            row = await db.get(
                SocialMemberPolicyRow,
                {
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "user_id": user_id,
                },
            )
            document = _member_document(tenant_id, session_id, user_id, row)
            control_row = await db.get(
                SocialTenantMemberControlRow,
                {"tenant_id": tenant_id, "user_id": user_id},
            )
            return _apply_tenant_member_control(document, control_row)

    async def put_member_policy(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        expected_version: int,
        update: MemberPrivacyPolicyUpdate,
        principal: Principal,
        idempotency_key: str = "",
        trace_id: str = "",
    ) -> MutationResult:
        resource_key = _resource_key(session_id, user_id)
        request_hash = _request_hash(expected_version, update.model_dump(mode="json"))
        async with self._session_factory() as db:
            async with db.begin():
                replay = await self._idempotent_replay(
                    db,
                    tenant_id=tenant_id,
                    resource_kind="member_policy",
                    resource_key=resource_key,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    document_type=MemberPrivacyPolicyDocument,
                )
                if replay is not None:
                    return MutationResult(document=replay, replayed=True)

                row = await db.scalar(
                    select(SocialMemberPolicyRow)
                    .where(
                        SocialMemberPolicyRow.tenant_id == tenant_id,
                        SocialMemberPolicyRow.session_id == session_id,
                        SocialMemberPolicyRow.user_id == user_id,
                    )
                    .with_for_update()
                )
                before = _member_document(tenant_id, session_id, user_id, row)
                if expected_version != before.version:
                    raise VersionConflictError(
                        expected=expected_version,
                        current=before.version,
                    )

                rollback_from: int | None = None
                if update.rollback_to_version is not None:
                    rollback_from = update.rollback_to_version
                    history = await db.scalar(
                        select(SocialMemberPolicyHistoryRow).where(
                            SocialMemberPolicyHistoryRow.tenant_id == tenant_id,
                            SocialMemberPolicyHistoryRow.session_id == session_id,
                            SocialMemberPolicyHistoryRow.user_id == user_id,
                            SocialMemberPolicyHistoryRow.version == rollback_from,
                        )
                    )
                    if history is None:
                        raise HistoryVersionNotFoundError(rollback_from)
                    policy = MemberPrivacyPolicyDocument.model_validate(
                        history.snapshot_json
                    ).policy
                else:
                    assert update.policy is not None
                    policy = update.policy

                now = datetime.now(UTC)
                new_version = before.version + 1
                if row is None:
                    row = SocialMemberPolicyRow(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        user_id=user_id,
                        version=new_version,
                    )
                    db.add(row)
                row.version = new_version
                row.policy_json = policy.model_dump(mode="json")
                row.updated_by = principal.subject
                row.updated_at = now

                stored_after = MemberPrivacyPolicyDocument(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    user_id=user_id,
                    version=new_version,
                    policy=policy,
                    updated_by=principal.subject,
                    updated_at=now,
                )
                control_row = await db.get(
                    SocialTenantMemberControlRow,
                    {"tenant_id": tenant_id, "user_id": user_id},
                )
                after = _apply_tenant_member_control(stored_after, control_row)
                after_json = after.model_dump(mode="json")
                history_json = stored_after.model_dump(mode="json")
                db.add(
                    SocialMemberPolicyHistoryRow(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        user_id=user_id,
                        version=new_version,
                        parent_version=before.version,
                        rollback_from_version=rollback_from,
                        snapshot_json=history_json,
                        actor=principal.subject,
                        change_reason=update.change_reason,
                    )
                )
                db.add(
                    _audit_row(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        user_id=user_id,
                        principal=principal,
                        action=(
                            "member_privacy_policy.rollback"
                            if rollback_from is not None
                            else "member_privacy_policy.update"
                        ),
                        target_type="social_member_policy",
                        before=before.model_dump(mode="json"),
                        after=after_json,
                        version=new_version,
                        trace_id=trace_id,
                        idempotency_key=idempotency_key,
                        reason=update.change_reason,
                    )
                )
                self._record_idempotency(
                    db,
                    tenant_id=tenant_id,
                    resource_kind="member_policy",
                    resource_key=resource_key,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    response=after_json,
                )
            return MutationResult(document=after)

    async def list_group_policy_history(
        self,
        *,
        tenant_id: str,
        session_id: str,
        limit: int = 50,
        cursor: str | None = None,
        voice_profile_only: bool = False,
    ) -> PolicyVersionPage:
        statement = select(SocialGroupPolicyHistoryRow).where(
            SocialGroupPolicyHistoryRow.tenant_id == tenant_id,
            SocialGroupPolicyHistoryRow.session_id == session_id,
        )
        cursor_value = _decode_cursor(cursor)
        if cursor_value is not None:
            created_at, row_id = cursor_value
            statement = statement.where(
                or_(
                    SocialGroupPolicyHistoryRow.created_at < created_at,
                    and_(
                        SocialGroupPolicyHistoryRow.created_at == created_at,
                        SocialGroupPolicyHistoryRow.id < row_id,
                    ),
                )
            )
        fetch_limit = max(1, min(int(limit or 50), 200))
        statement = statement.order_by(
            desc(SocialGroupPolicyHistoryRow.created_at),
            desc(SocialGroupPolicyHistoryRow.id),
        ).limit(fetch_limit + 1)
        async with self._session_factory() as db:
            rows = list((await db.scalars(statement)).all())
            has_more = len(rows) > fetch_limit
            page_rows = rows[:fetch_limit]
            items: list[PolicyVersionMetadata] = []
            for row in page_rows:
                parent = None
                if row.parent_version > 0:
                    parent = await db.scalar(
                        select(SocialGroupPolicyHistoryRow).where(
                            SocialGroupPolicyHistoryRow.tenant_id == tenant_id,
                            SocialGroupPolicyHistoryRow.session_id == session_id,
                            SocialGroupPolicyHistoryRow.version == row.parent_version,
                        )
                    )
                summary = _history_change_summary(
                    row.snapshot_json,
                    parent.snapshot_json if parent is not None else {},
                    kind="group",
                    rollback_from=row.rollback_from_version,
                )
                if voice_profile_only and not any(
                    item.startswith("voice_profile") for item in summary
                ):
                    continue
                items.append(_history_metadata(row, summary))
        next_cursor = (
            _encode_cursor(page_rows[-1].created_at, page_rows[-1].id)
            if has_more and page_rows
            else None
        )
        return PolicyVersionPage(items=items, next_cursor=next_cursor)

    async def list_member_policy_history(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> PolicyVersionPage:
        statement = select(SocialMemberPolicyHistoryRow).where(
            SocialMemberPolicyHistoryRow.tenant_id == tenant_id,
            SocialMemberPolicyHistoryRow.session_id == session_id,
            SocialMemberPolicyHistoryRow.user_id == user_id,
        )
        cursor_value = _decode_cursor(cursor)
        if cursor_value is not None:
            created_at, row_id = cursor_value
            statement = statement.where(
                or_(
                    SocialMemberPolicyHistoryRow.created_at < created_at,
                    and_(
                        SocialMemberPolicyHistoryRow.created_at == created_at,
                        SocialMemberPolicyHistoryRow.id < row_id,
                    ),
                )
            )
        fetch_limit = max(1, min(int(limit or 50), 200))
        statement = statement.order_by(
            desc(SocialMemberPolicyHistoryRow.created_at),
            desc(SocialMemberPolicyHistoryRow.id),
        ).limit(fetch_limit + 1)
        async with self._session_factory() as db:
            rows = list((await db.scalars(statement)).all())
            has_more = len(rows) > fetch_limit
            page_rows = rows[:fetch_limit]
            items: list[PolicyVersionMetadata] = []
            for row in page_rows:
                parent = None
                if row.parent_version > 0:
                    parent = await db.scalar(
                        select(SocialMemberPolicyHistoryRow).where(
                            SocialMemberPolicyHistoryRow.tenant_id == tenant_id,
                            SocialMemberPolicyHistoryRow.session_id == session_id,
                            SocialMemberPolicyHistoryRow.user_id == user_id,
                            SocialMemberPolicyHistoryRow.version == row.parent_version,
                        )
                    )
                summary = _history_change_summary(
                    row.snapshot_json,
                    parent.snapshot_json if parent is not None else {},
                    kind="member",
                    rollback_from=row.rollback_from_version,
                )
                items.append(_history_metadata(row, summary))
        next_cursor = (
            _encode_cursor(page_rows[-1].created_at, page_rows[-1].id)
            if has_more and page_rows
            else None
        )
        return PolicyVersionPage(items=items, next_cursor=next_cursor)

    async def list_voice_profile_history(
        self,
        *,
        tenant_id: str,
        session_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> PolicyVersionPage:
        statement = select(VoiceProfileHistoryRow).where(
            VoiceProfileHistoryRow.tenant_id == tenant_id,
            VoiceProfileHistoryRow.session_id == session_id,
        )
        cursor_value = _decode_cursor(cursor)
        if cursor_value is not None:
            created_at, row_id = cursor_value
            statement = statement.where(
                or_(
                    VoiceProfileHistoryRow.created_at < created_at,
                    and_(
                        VoiceProfileHistoryRow.created_at == created_at,
                        VoiceProfileHistoryRow.id < row_id,
                    ),
                )
            )
        fetch_limit = max(1, min(int(limit or 50), 200))
        statement = statement.order_by(
            desc(VoiceProfileHistoryRow.created_at),
            desc(VoiceProfileHistoryRow.id),
        ).limit(fetch_limit + 1)
        async with self._session_factory() as db:
            rows = list((await db.scalars(statement)).all())
        has_more = len(rows) > fetch_limit
        page_rows = rows[:fetch_limit]
        items = []
        for row in page_rows:
            snapshot = row.snapshot_json if isinstance(row.snapshot_json, dict) else {}
            profile_summary = (
                "voice_profile:removed"
                if snapshot.get("removed") is True
                else f"voice_profile:{row.profile_id}"
            )
            items.append(
                _history_metadata(
                    row,
                    [
                        *(
                            [f"rollback:v{row.rollback_from_version}"]
                            if row.rollback_from_version is not None
                            else []
                        ),
                        profile_summary,
                    ],
                )
            )
        return PolicyVersionPage(
            items=items,
            next_cursor=(
                _encode_cursor(page_rows[-1].created_at, page_rows[-1].id)
                if has_more and page_rows
                else None
            ),
        )

    async def record_participation_event(
        self,
        *,
        tenant_id: str,
        session_id: str,
        policy_version: int,
        event_kind: str,
        decision: ParticipationDecision,
        signal_summary: dict[str, bool | int | float | str],
        trace_id: str = "",
        runtime_stage: str = "decision",
        delivery_stage: str = "not_applicable",
    ) -> ParticipationEventDocument:
        row = SocialParticipationEventRow(
            tenant_id=tenant_id,
            session_id=session_id,
            policy_version=policy_version,
            event_kind=event_kind,
            runtime_stage=str(runtime_stage or "decision")[:32],
            delivery_stage=str(delivery_stage or "not_applicable")[:32],
            status=decision.status.value,
            score=decision.score,
            reason_codes_json=list(decision.reason_codes),
            signal_summary_json=dict(signal_summary),
            trace_id=trace_id,
        )
        async with self._session_factory() as db:
            async with db.begin():
                db.add(row)
                await db.flush()
                document = _event_document(row)
        return document

    async def record_natural_feedback_audit(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        action: str,
        principal: Principal,
        idempotency_key: str,
        trace_id: str = "",
    ) -> bool:
        """Durably audit memory-only feedback once without storing chat text."""

        resource_key = _resource_key(session_id, user_id, action)
        request_hash = _request_hash(0, {"action": action})
        async with self._session_factory() as db:
            async with db.begin():
                existing = await db.scalar(
                    select(SocialPolicyIdempotencyRow).where(
                        SocialPolicyIdempotencyRow.tenant_id == tenant_id,
                        SocialPolicyIdempotencyRow.resource_kind == "natural_feedback",
                        SocialPolicyIdempotencyRow.resource_key == resource_key,
                        SocialPolicyIdempotencyRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise IdempotencyConflictError(
                            "idempotency key was used for another request"
                        )
                    return False
                db.add(
                    _audit_row(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        user_id=user_id,
                        principal=principal,
                        action=f"natural_feedback.{action}",
                        target_type="member_memory",
                        before={},
                        after={"requested": True},
                        version=0,
                        trace_id=trace_id,
                        idempotency_key=idempotency_key,
                        reason=f"natural_feedback:{action}",
                    )
                )
                self._record_idempotency(
                    db,
                    tenant_id=tenant_id,
                    resource_kind="natural_feedback",
                    resource_key=resource_key,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    response={"recorded": True},
                )
        return True

    async def member_memory_mutation_replayed(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        item_id: int,
        action: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
    ) -> bool:
        request_hash = _request_hash(0, request_payload)
        resource_kind = f"memory_{action}"[:32]
        resource_key = _resource_key(session_id, user_id, str(item_id))
        async with self._session_factory() as db:
            row = await db.scalar(
                select(SocialPolicyIdempotencyRow).where(
                    SocialPolicyIdempotencyRow.tenant_id == tenant_id,
                    SocialPolicyIdempotencyRow.resource_kind == resource_kind,
                    SocialPolicyIdempotencyRow.resource_key == resource_key,
                    SocialPolicyIdempotencyRow.idempotency_key == idempotency_key,
                )
            )
        if row is None:
            return False
        if row.request_hash != request_hash:
            raise IdempotencyConflictError("idempotency key was used for another request")
        return True

    async def record_member_memory_mutation(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        item_id: int,
        action: str,
        principal: Principal,
        idempotency_key: str,
        trace_id: str,
        request_payload: dict[str, Any],
        before_metadata: dict[str, Any],
        after_metadata: dict[str, Any],
        reason: str = "",
    ) -> None:
        request_hash = _request_hash(0, request_payload)
        resource_kind = f"memory_{action}"[:32]
        resource_key = _resource_key(session_id, user_id, str(item_id))
        async with self._session_factory() as db:
            async with db.begin():
                existing = await db.scalar(
                    select(SocialPolicyIdempotencyRow).where(
                        SocialPolicyIdempotencyRow.tenant_id == tenant_id,
                        SocialPolicyIdempotencyRow.resource_kind == resource_kind,
                        SocialPolicyIdempotencyRow.resource_key == resource_key,
                        SocialPolicyIdempotencyRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise IdempotencyConflictError(
                            "idempotency key was used for another request"
                        )
                    return
                db.add(
                    _audit_row(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        user_id=user_id,
                        principal=principal,
                        action=f"member_memory.{action}",
                        target_type="member_memory_item",
                        before=before_metadata,
                        after=after_metadata,
                        version=0,
                        trace_id=trace_id,
                        idempotency_key=idempotency_key,
                        reason=reason,
                    )
                )
                self._record_idempotency(
                    db,
                    tenant_id=tenant_id,
                    resource_kind=resource_kind,
                    resource_key=resource_key,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    response={"item_id": item_id, "action": action},
                )

    async def list_participation_events(
        self,
        *,
        tenant_id: str,
        session_id: str,
        limit: int = 50,
        before: datetime | None = None,
        cursor: str | None = None,
        status: str | None = None,
        source: str | None = None,
        version: int | None = None,
        reason: str | None = None,
        runtime_stage: str | None = None,
        delivery_stage: str | None = None,
    ) -> ParticipationEventPage:
        statement = select(SocialParticipationEventRow).where(
            SocialParticipationEventRow.tenant_id == tenant_id,
            SocialParticipationEventRow.session_id == session_id,
        )
        if before is not None:
            statement = statement.where(SocialParticipationEventRow.created_at < before)
        cursor_value = _decode_cursor(cursor)
        if cursor_value is not None:
            created_at, row_id = cursor_value
            statement = statement.where(
                or_(
                    SocialParticipationEventRow.created_at < created_at,
                    and_(
                        SocialParticipationEventRow.created_at == created_at,
                        SocialParticipationEventRow.id < row_id,
                    ),
                )
            )
        if status:
            statement = statement.where(SocialParticipationEventRow.status == status)
        if source:
            statement = statement.where(SocialParticipationEventRow.event_kind == source)
        if version is not None:
            statement = statement.where(SocialParticipationEventRow.policy_version == version)
        if reason:
            # Reason codes are compact controlled identifiers. Quoting the
            # needle prevents prefix matches (for example ``quiet_hours`` must
            # not match ``quiet_hours_at_send``) across SQLite and PostgreSQL.
            escaped_reason = reason.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            statement = statement.where(
                sql_cast(SocialParticipationEventRow.reason_codes_json, String).like(
                    f'%"{escaped_reason}"%', escape="\\"
                )
            )
        if runtime_stage:
            statement = statement.where(
                SocialParticipationEventRow.runtime_stage == runtime_stage
            )
        if delivery_stage:
            statement = statement.where(
                SocialParticipationEventRow.delivery_stage == delivery_stage
            )
        fetch_limit = max(1, min(int(limit or 50), 200))
        statement = statement.order_by(
            desc(SocialParticipationEventRow.created_at),
            desc(SocialParticipationEventRow.id),
        ).limit(fetch_limit + 1)
        async with self._session_factory() as db:
            rows = list((await db.scalars(statement)).all())
        has_more = len(rows) > fetch_limit
        page_rows = rows[:fetch_limit]
        items = [_event_document(row) for row in page_rows]
        next_before = items[-1].created_at if has_more and items else None
        next_cursor = (
            _encode_cursor(page_rows[-1].created_at, page_rows[-1].id)
            if has_more and page_rows
            else None
        )
        return ParticipationEventPage(
            items=items,
            next_before=next_before,
            next_cursor=next_cursor,
        )

    async def _group_document(
        self,
        db: AsyncSession,
        tenant_id: str,
        session_id: str,
        row: SocialGroupPolicyRow | None,
    ) -> GroupParticipationPolicyDocument:
        # Preserve the baseline group-reply behavior for newly discovered
        # groups.  Only an explicit, persisted group override may disable
        # participation; the independent global and tenant gates still apply.
        group_enabled = True if row is None else bool(row.group_enabled)
        switches = await self._effective_switches(
            db,
            tenant_id=tenant_id,
            group_enabled=group_enabled,
        )
        if row is None:
            return GroupParticipationPolicyDocument(
                tenant_id=tenant_id,
                session_id=session_id,
                version=0,
                kill_switches=switches,
                effective_enabled=switches.effective_enabled,
                policy=ParticipationPolicyValues(
                    rollout_stage=cast(
                        RolloutStage,
                        await self._effective_rollout_stage(db, tenant_id),
                    )
                ),
            )
        voice: VoiceProfile | None = None
        if row.voice_profile_id:
            voice_row = await db.get(
                VoiceProfileRow,
                {
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "profile_id": row.voice_profile_id,
                },
            )
            if voice_row is not None:
                voice = VoiceProfile.model_validate(
                    {**voice_row.profile_json, "version": voice_row.version}
                )
        return GroupParticipationPolicyDocument(
            tenant_id=tenant_id,
            session_id=session_id,
            version=row.version,
            kill_switches=switches,
            effective_enabled=switches.effective_enabled,
            policy=ParticipationPolicyValues.model_validate(row.policy_json).model_copy(
                update={"rollout_stage": await self._effective_rollout_stage(db, tenant_id)}
            ),
            voice_profile=voice,
            updated_by=row.updated_by,
            updated_at=row.updated_at,
        )

    @staticmethod
    async def _effective_switches(
        db: AsyncSession,
        *,
        tenant_id: str,
        group_enabled: bool,
    ) -> KillSwitches:
        release = await db.get(
            SocialScopeControlRow,
            {"scope_kind": "global", "tenant_id": ""},
        )
        tenant = await db.get(
            SocialScopeControlRow,
            {"scope_kind": "tenant", "tenant_id": tenant_id},
        )
        return KillSwitches(
            global_enabled=bool(release.enabled) if release is not None else False,
            tenant_enabled=bool(tenant.enabled) if tenant is not None else False,
            group_enabled=group_enabled,
        )

    @staticmethod
    async def _effective_rollout_stage(db: AsyncSession, tenant_id: str) -> str:
        release = await db.get(
            SocialScopeControlRow,
            {"scope_kind": "global", "tenant_id": ""},
        )
        tenant = await db.get(
            SocialScopeControlRow,
            {"scope_kind": "tenant", "tenant_id": tenant_id},
        )
        return _most_restrictive_stage(
            str(release.rollout_stage) if release is not None else "shadow",
            str(tenant.rollout_stage) if tenant is not None else "shadow",
        )

    @staticmethod
    async def _upsert_voice_profile(
        db: AsyncSession,
        *,
        tenant_id: str,
        session_id: str,
        profile: VoiceProfile,
        parent_version: int,
        rollback_from_version: int | None,
        actor: str,
        change_reason: str,
        now: datetime,
    ) -> None:
        db.add(
            VoiceProfileHistoryRow(
                tenant_id=tenant_id,
                session_id=session_id,
                profile_id=profile.profile_id,
                version=profile.version,
                parent_version=parent_version,
                rollback_from_version=rollback_from_version,
                snapshot_json=profile.model_dump(mode="json"),
                actor=actor,
                change_reason=change_reason,
                created_at=now,
            )
        )
        row = await db.get(
            VoiceProfileRow,
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "profile_id": profile.profile_id,
            },
        )
        if row is None:
            row = VoiceProfileRow(
                tenant_id=tenant_id,
                session_id=session_id,
                profile_id=profile.profile_id,
            )
            db.add(row)
        row.version = profile.version
        row.profile_json = profile.model_dump(mode="json")
        row.updated_by = actor
        row.updated_at = now

    @staticmethod
    async def _idempotent_replay(
        db: AsyncSession,
        *,
        tenant_id: str,
        resource_kind: str,
        resource_key: str,
        idempotency_key: str,
        request_hash: str,
        document_type: type[Any],
    ) -> Any | None:
        if not idempotency_key:
            return None
        row = await db.scalar(
            select(SocialPolicyIdempotencyRow).where(
                SocialPolicyIdempotencyRow.tenant_id == tenant_id,
                SocialPolicyIdempotencyRow.resource_kind == resource_kind,
                SocialPolicyIdempotencyRow.resource_key == resource_key,
                SocialPolicyIdempotencyRow.idempotency_key == idempotency_key,
            )
        )
        if row is None:
            return None
        if row.request_hash != request_hash:
            raise IdempotencyConflictError("idempotency key was used for another request")
        return document_type.model_validate(row.response_json)

    @staticmethod
    def _record_idempotency(
        db: AsyncSession,
        *,
        tenant_id: str,
        resource_kind: str,
        resource_key: str,
        idempotency_key: str,
        request_hash: str,
        response: dict[str, Any],
    ) -> None:
        if not idempotency_key:
            return
        db.add(
            SocialPolicyIdempotencyRow(
                tenant_id=tenant_id,
                resource_kind=resource_kind,
                resource_key=resource_key,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response_json=response,
            )
        )


def _member_document(
    tenant_id: str,
    session_id: str,
    user_id: str,
    row: SocialMemberPolicyRow | None,
) -> MemberPrivacyPolicyDocument:
    if row is None:
        return MemberPrivacyPolicyDocument(
            tenant_id=tenant_id,
            session_id=session_id,
            user_id=user_id,
            version=0,
            policy=MemberPrivacyValues(),
        )
    return MemberPrivacyPolicyDocument(
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=user_id,
        version=row.version,
        policy=MemberPrivacyValues.model_validate(row.policy_json),
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


def _event_document(row: SocialParticipationEventRow) -> ParticipationEventDocument:
    return ParticipationEventDocument(
        event_id=row.id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        policy_version=row.policy_version,
        event_kind=cast(ParticipationEventKind, row.event_kind),
        runtime_stage=row.runtime_stage,
        delivery_stage=row.delivery_stage,
        status=cast(ParticipationStatus, row.status),
        score=row.score,
        reason_codes=list(row.reason_codes_json),
        signal_summary=dict(row.signal_summary_json),
        trace_id=row.trace_id,
        created_at=row.created_at,
    )


def _audit_row(
    *,
    tenant_id: str,
    session_id: str,
    user_id: str,
    principal: Principal,
    action: str,
    target_type: str,
    before: dict[str, Any],
    after: dict[str, Any],
    version: int,
    trace_id: str,
    idempotency_key: str,
    reason: str,
) -> AuditEventRow:
    return AuditEventRow(
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=user_id,
        actor=principal.subject,
        actor_kind=principal.auth_kind,
        action=action,
        target_type=target_type,
        before_state_json=before,
        after_state_json=after,
        policy_version=version,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        reason=reason,
    )


def _require_voice_profile_scope(
    profile: VoiceProfile,
    *,
    session_id: str,
) -> None:
    if profile.sample_authorization_reason(session_id):
        raise VoiceProfileScopeError()


def _request_hash(expected_version: int, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"expected_version": expected_version, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resource_key(*parts: str) -> str:
    """Encode composite opaque identifiers without delimiter collisions."""

    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def _member_control_session_id(tenant_id: str, user_id: str) -> str:
    material = f"{tenant_id}\x00{user_id}".encode()
    return f"admin-member-control:{hashlib.sha256(material).hexdigest()[:32]}"


def _scope_control_document(
    scope: ParticipationControlScope,
    tenant_id: str,
    row: SocialScopeControlRow | None,
) -> ScopedParticipationControlDocument:
    if row is None:
        return ScopedParticipationControlDocument(scope=scope, tenant_id=tenant_id)
    return ScopedParticipationControlDocument(
        scope=scope,
        tenant_id=tenant_id,
        version=row.version,
        control=ScopedParticipationControlValues(
            enabled=row.enabled,
            rollout_stage=cast(RolloutStage, row.rollout_stage),
        ),
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


def _tenant_member_document(
    tenant_id: str,
    user_id: str,
    row: SocialTenantMemberControlRow | None,
) -> TenantMemberControlDocument:
    if row is None:
        return TenantMemberControlDocument(tenant_id=tenant_id, user_id=user_id)
    return TenantMemberControlDocument(
        tenant_id=tenant_id,
        user_id=user_id,
        version=row.version,
        control=TenantMemberControlValues(
            memory_opt_out=row.memory_opt_out,
            participation_opt_out=row.participation_opt_out,
            no_group_mentions=row.no_group_mentions,
        ),
        deletion_state=cast(MemberDeletionState, row.deletion_state),
        deletion_intent_key=row.deletion_intent_key,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


def _apply_tenant_member_control(
    document: MemberPrivacyPolicyDocument,
    control_row: SocialTenantMemberControlRow | None,
) -> MemberPrivacyPolicyDocument:
    configured_policy = document.configured_policy or document.policy
    if control_row is None:
        return document.model_copy(update={"configured_policy": configured_policy})
    policy = configured_policy
    values = policy.model_dump()
    if control_row.memory_opt_out:
        values.update(
            memory_enabled=False,
            allow_group_recall=False,
            allow_private_recall=False,
            sensitive_memory_enabled=False,
        )
    if control_row.participation_opt_out:
        values.update(
            proactive_participation_enabled=False,
            soft_reply_opt_out=True,
        )
    if control_row.no_group_mentions:
        values["no_group_mentions"] = True
    return document.model_copy(
        update={
            "configured_policy": configured_policy,
            "policy": MemberPrivacyValues.model_validate(values),
        }
    )


def _most_restrictive_stage(*stages: str) -> str:
    order = {
        "shadow": 0,
        "privacy_5": 1,
        "style_10": 2,
        "contextual": 3,
        "proactive": 4,
    }
    normalized = [stage if stage in order else "shadow" for stage in stages]
    return min(normalized, key=order.__getitem__, default="shadow")


def _encode_cursor(created_at: datetime, row_id: str) -> str:
    payload = json.dumps(
        {"created_at": created_at.isoformat(), "id": row_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    normalized = str(cursor or "").strip()
    if not normalized:
        return None
    try:
        padded = normalized + "=" * (-len(normalized) % 4)
        payload = json.loads(urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        created_at = datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
        row_id = str(payload["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_cursor") from exc
    if not row_id:
        raise ValueError("invalid_cursor")
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return created_at, row_id


def _history_metadata(row: Any, summary: list[str]) -> PolicyVersionMetadata:
    return PolicyVersionMetadata(
        version=row.version,
        parent_version=row.parent_version,
        rollback_from_version=row.rollback_from_version,
        actor=row.actor,
        change_summary=summary,
        reason_present=bool(str(row.change_reason or "").strip()),
        created_at=row.created_at,
    )


def _history_change_summary(
    current: dict[str, Any],
    parent: dict[str, Any],
    *,
    kind: str,
    rollback_from: int | None,
) -> list[str]:
    summary: list[str] = []
    if rollback_from is not None:
        summary.append(f"rollback:v{rollback_from}")
    if kind == "group":
        current_switches = dict(current.get("kill_switches") or {})
        parent_switches = dict(parent.get("kill_switches") or {})
        for key in sorted(set(current_switches) | set(parent_switches)):
            if current_switches.get(key) != parent_switches.get(key):
                summary.append(f"kill_switch:{key}")
        current_policy = dict(current.get("policy") or {})
        parent_policy = dict(parent.get("policy") or {})
        for key in sorted(set(current_policy) | set(parent_policy)):
            if current_policy.get(key) != parent_policy.get(key):
                summary.append(f"policy:{key}")
        if current.get("voice_profile") != parent.get("voice_profile"):
            summary.append("voice_profile:changed")
    else:
        current_policy = dict(current.get("policy") or {})
        parent_policy = dict(parent.get("policy") or {})
        for key in sorted(set(current_policy) | set(parent_policy)):
            if current_policy.get(key) != parent_policy.get(key):
                summary.append(f"privacy:{key}")
    return summary or ["metadata:no_effective_change"]
