"""Natural-language control commands for group participation and privacy."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.admin.authorization import Principal
from app.social.contracts import (
    MemberMemoryCorrectionResolution,
    MemberPrivacyPolicyDocument,
    MemberPrivacyPolicyUpdate,
    TenantMemberControlDocument,
    TenantMemberControlUpdate,
    TenantMemberControlValues,
)
from app.social.telemetry import observe_privacy_action

_CLAUSE_SPLIT_RE = re.compile(r"[，。！？、,.!?；;：:\n]+")
_DIRECT_PREFIX_RE = re.compile(r"^(?:请|麻烦|以后|从现在起|机器人)?(?:也)?")
_DIRECT_SUFFIX_RE = re.compile(r"(?:了|吧|哈|谢谢|可以吗|行吗|就行)?$")
_REPORTED_SPEECH_MARKERS = ("他说", "她说", "有人说", "转发", "引用", "是什么意思")


class NaturalFeedbackAction(StrEnum):
    REDUCE_REPLIES = "reduce_replies"
    DISABLE_PROACTIVE = "disable_proactive"
    FORGET_MEMBER = "forget_member"
    KEEP_OUT_OF_GROUP = "keep_out_of_group"
    CORRECT_MEMORY = "correct_memory"


@dataclass(frozen=True, slots=True)
class NaturalFeedbackSignal:
    action: NaturalFeedbackAction
    phrase: str


@dataclass(frozen=True, slots=True)
class NaturalFeedbackResult:
    signal: NaturalFeedbackSignal
    applied: bool
    policy_version: int = 0
    memory_items_changed: int = 0
    memory_action_pending: bool = False
    memory_confirmation_required: bool = False
    memory_candidate_count: int = 0


class MemberPolicyPort(Protocol):
    async def get_member_policy(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
    ) -> MemberPrivacyPolicyDocument: ...

    async def put_member_policy(self, **kwargs: object) -> object: ...

    async def get_tenant_member_control(
        self,
        tenant_id: str,
        user_id: str,
    ) -> TenantMemberControlDocument: ...

    async def put_tenant_member_control(self, **kwargs: object) -> object: ...


class MemoryFeedbackPort(Protocol):
    async def forget_member(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        idempotency_key: str,
    ) -> int: ...

    async def resolve_member_fact_correction(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        correction_text: str,
        idempotency_key: str,
    ) -> MemberMemoryCorrectionResolution: ...


_PHRASES: tuple[tuple[NaturalFeedbackAction, tuple[str, ...]], ...] = (
    (
        NaturalFeedbackAction.FORGET_MEMBER,
        ("别记我", "不要记我", "别记录我", "不要记录我", "忘掉我"),
    ),
    (
        NaturalFeedbackAction.KEEP_OUT_OF_GROUP,
        ("群里别提", "群里不要提", "不要在群里提", "别在群里说"),
    ),
    (
        NaturalFeedbackAction.DISABLE_PROACTIVE,
        ("别主动", "不要主动", "别主动说话", "不要主动发言"),
    ),
    (
        NaturalFeedbackAction.REDUCE_REPLIES,
        ("少说点", "少说一点", "少插嘴", "话少一点"),
    ),
    (
        NaturalFeedbackAction.CORRECT_MEMORY,
        ("你记错了", "这个记错了", "那不是我", "记错了"),
    ),
)


def detect_natural_feedback(text: str) -> tuple[NaturalFeedbackSignal, ...]:
    """Detect short, direct member controls without treating quoted prose as policy."""

    raw = str(text or "").strip()
    if not raw or len(raw) > 80:
        return ()
    clauses = [
        clause.strip().lower()
        for clause in _CLAUSE_SPLIT_RE.split(raw)
        if clause.strip()
    ]
    signals: list[NaturalFeedbackSignal] = []
    for action, phrases in _PHRASES:
        match = next(
            (
                phrase
                for clause in clauses
                if _is_direct_control_clause(clause, action)
                for phrase in phrases
                if _clause_matches_phrase(clause, phrase, action)
            ),
            "",
        )
        if match:
            signals.append(NaturalFeedbackSignal(action=action, phrase=match))
    return tuple(signals)


def _is_direct_control_clause(
    clause: str,
    action: NaturalFeedbackAction,
) -> bool:
    if not clause or len(clause) > 32:
        return False
    if any(marker in clause for marker in _REPORTED_SPEECH_MARKERS):
        return False
    if any(mark in clause for mark in ('"', "'", "“", "”", "‘", "’")):
        return False
    # A correction commonly carries the corrected fact in the following
    # clause, so only the control clause itself must stay short and direct.
    return action is NaturalFeedbackAction.CORRECT_MEMORY or not clause.endswith("吗")


def _clause_matches_phrase(
    clause: str,
    phrase: str,
    action: NaturalFeedbackAction,
) -> bool:
    normalized = _DIRECT_PREFIX_RE.sub("", clause, count=1)
    if action is NaturalFeedbackAction.KEEP_OUT_OF_GROUP:
        normalized = re.sub(r"(?:我|这些|这件事)$", "", normalized)
    if normalized == phrase:
        return True
    return _DIRECT_SUFFIX_RE.sub("", normalized, count=1) == phrase


def _feedback_idempotency_key(
    message_id: str,
    action: NaturalFeedbackAction,
) -> str:
    digest = hashlib.sha256(str(message_id or "").encode()).hexdigest()[:32]
    return f"natural-feedback:{action.value}:{digest}"


class NaturalFeedbackService:
    """Apply detected controls through the versioned, auditable policy surface."""

    def __init__(
        self,
        policy_store: MemberPolicyPort,
        *,
        memory: MemoryFeedbackPort | None = None,
    ) -> None:
        self._policy_store = policy_store
        self._memory = memory

    async def get_member_policy(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
    ) -> MemberPrivacyPolicyDocument:
        return await self._policy_store.get_member_policy(
            tenant_id,
            session_id,
            user_id,
        )

    async def apply(
        self,
        signal: NaturalFeedbackSignal,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        message_id: str,
        correction_text: str = "",
        trace_id: str = "",
    ) -> NaturalFeedbackResult:
        if signal.action == NaturalFeedbackAction.CORRECT_MEMORY:
            return await self._apply_memory_correction(
                signal,
                tenant_id=tenant_id,
                session_id=session_id,
                user_id=user_id,
                message_id=message_id,
                correction_text=correction_text,
                trace_id=trace_id,
            )

        current = await self._policy_store.get_member_policy(
            tenant_id,
            session_id,
            user_id,
        )
        configured_policy = current.configured_policy or current.policy
        policy = configured_policy.model_copy(deep=True)
        if signal.action == NaturalFeedbackAction.REDUCE_REPLIES:
            policy.soft_reply_opt_out = True
        elif signal.action == NaturalFeedbackAction.DISABLE_PROACTIVE:
            policy.proactive_participation_enabled = False
        elif signal.action == NaturalFeedbackAction.FORGET_MEMBER:
            policy.memory_enabled = False
            policy.allow_group_recall = False
            policy.allow_private_recall = False
            policy.sensitive_memory_enabled = False
            policy.audience_scope = "private"
            policy.allowed_session_ids = []
        elif signal.action == NaturalFeedbackAction.KEEP_OUT_OF_GROUP:
            policy.allow_group_recall = False
            policy.no_group_mentions = True
            policy.audience_scope = "private"
            policy.allowed_session_ids = []

        idempotency_key = _feedback_idempotency_key(message_id, signal.action)
        deletion_pending = False
        durable_control = False
        if signal.action is NaturalFeedbackAction.FORGET_MEMBER:
            get_control = getattr(self._policy_store, "get_tenant_member_control", None)
            put_control = getattr(self._policy_store, "put_tenant_member_control", None)
            if callable(get_control) and callable(put_control):
                current_control = await get_control(tenant_id, user_id)
                expected_intent = f"member-memory-delete:{idempotency_key}"[:512]
                if current_control.deletion_intent_key != expected_intent:
                    mutation = await put_control(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        expected_version=current_control.version,
                        update=TenantMemberControlUpdate(
                            control=TenantMemberControlValues(
                                memory_opt_out=True,
                                participation_opt_out=(
                                    current_control.control.participation_opt_out
                                ),
                                no_group_mentions=current_control.control.no_group_mentions,
                            ),
                            request_memory_deletion=True,
                            change_reason="natural_feedback:forget_member",
                        ),
                        principal=Principal(
                            subject=f"member:{user_id}",
                            roles=(),
                            tenant_ids=(tenant_id,),
                            auth_kind="natural_feedback",
                        ),
                        idempotency_key=idempotency_key,
                        trace_id=trace_id,
                    )
                    current_control = getattr(mutation, "document", mutation)
                deletion_pending = current_control.deletion_state != "completed"
                durable_control = True
        if policy == configured_policy:
            # A replay observes the already-committed fail-closed state.  Do
            # not create a second policy version or reuse the original key with
            # a different optimistic-lock version.
            policy_version = current.version
        else:
            mutation = await self._policy_store.put_member_policy(
                tenant_id=tenant_id,
                session_id=session_id,
                user_id=user_id,
                expected_version=current.version,
                update=MemberPrivacyPolicyUpdate(
                    policy=policy,
                    change_reason=f"natural_feedback:{signal.action.value}",
                ),
                principal=Principal(
                    subject=f"member:{user_id}",
                    roles=(),
                    tenant_ids=(tenant_id,),
                    auth_kind="natural_feedback",
                ),
                idempotency_key=idempotency_key,
                trace_id=trace_id,
            )
            document = getattr(mutation, "document", mutation)
            policy_version = int(getattr(document, "version", current.version + 1))

        changed = 0
        pending = deletion_pending
        if signal.action == NaturalFeedbackAction.FORGET_MEMBER:
            if durable_control:
                # The effect-intent relay owns physical deletion.  Returning a
                # pending result prevents a synchronous best-effort delete from
                # being mistaken for durable completion.
                pass
            elif self._memory is None:
                pending = True
            else:
                try:
                    changed = await self._memory.forget_member(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        user_id=user_id,
                        idempotency_key=idempotency_key,
                    )
                except Exception:
                    # The fail-closed policy is already committed, so no new
                    # memory or recall can occur while durable deletion retries.
                    pending = True

        result = NaturalFeedbackResult(
            signal=signal,
            applied=True,
            policy_version=policy_version,
            memory_items_changed=changed,
            memory_action_pending=pending,
        )
        observe_privacy_action(
            signal.action.value,
            succeeded=result.applied and not result.memory_action_pending,
        )
        return result

    async def _apply_memory_correction(
        self,
        signal: NaturalFeedbackSignal,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        message_id: str,
        correction_text: str,
        trace_id: str,
    ) -> NaturalFeedbackResult:
        idempotency_key = _feedback_idempotency_key(message_id, signal.action)
        audit = getattr(self._policy_store, "record_natural_feedback_audit", None)
        if callable(audit):
            await audit(
                tenant_id=tenant_id,
                session_id=session_id,
                user_id=user_id,
                action=signal.action.value,
                principal=Principal(
                    subject=f"member:{user_id}",
                    roles=(),
                    tenant_ids=(tenant_id,),
                    auth_kind="natural_feedback",
                ),
                idempotency_key=idempotency_key,
                trace_id=trace_id,
            )
        if self._memory is None:
            result = NaturalFeedbackResult(
                signal=signal,
                applied=False,
                memory_action_pending=True,
            )
            observe_privacy_action(signal.action.value, succeeded=False)
            return result
        try:
            resolution = await self._memory.resolve_member_fact_correction(
                tenant_id=tenant_id,
                session_id=session_id,
                user_id=user_id,
                correction_text=str(correction_text or "").strip(),
                idempotency_key=idempotency_key,
            )
        except Exception:
            result = NaturalFeedbackResult(
                signal=signal,
                applied=False,
                memory_action_pending=True,
            )
            observe_privacy_action(signal.action.value, succeeded=False)
            return result
        normalized = MemberMemoryCorrectionResolution.model_validate(resolution)
        result = NaturalFeedbackResult(
            signal=signal,
            applied=normalized.status == "applied" and normalized.changed > 0,
            memory_items_changed=normalized.changed,
            memory_confirmation_required=normalized.status == "confirmation_required",
            memory_candidate_count=normalized.candidate_count,
        )
        observe_privacy_action(signal.action.value, succeeded=result.applied)
        return result
