"""Durable social/privacy effect handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.common.types import Channel, MessageType
from app.orchestrator.effects import EffectCommitRecord
from app.orchestrator.flow import MessageEffect
from app.orchestrator.pipeline import PipelineContext
from app.social.contracts import MemberMemoryForgetEffectPayload
from app.social.store import SocialPolicyStore

MEMBER_MEMORY_ERASURE_OWNER = "core"
LEGACY_MEMBER_MEMORY_ERASURE_OWNER = "memory"


class MemberMemoryDeletionPort(Protocol):
    async def forget_member(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        idempotency_key: str,
    ) -> int: ...


@dataclass(slots=True)
class MemberMemoryForgetEffectHandler:
    """Erase all audience variants for one exact tenant/member identity."""

    memory_store: MemberMemoryDeletionPort
    policy_store: SocialPolicyStore

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        _ = record
        if effect.owner not in {
            MEMBER_MEMORY_ERASURE_OWNER,
            LEGACY_MEMBER_MEMORY_ERASURE_OWNER,
        } or effect.type != "forget_member":
            raise ValueError("invalid member-memory erasure effect identity")
        payload = MemberMemoryForgetEffectPayload.model_validate(effect.payload)
        event = ctx.event
        if (
            event.tenant_id != payload.tenant_id
            or event.user_id != payload.user_id
            or event.channel is not Channel.WECHAT
            or event.message.type is not MessageType.EVENT
            or event.message.content
            or payload.deletion_intent_key != effect.idempotency_key
        ):
            raise ValueError("member-memory erasure scope mismatch")

        try:
            await self.memory_store.forget_member(
                tenant_id=payload.tenant_id,
                session_id=event.session_id,
                user_id=payload.user_id,
                idempotency_key=effect.idempotency_key,
            )
        except Exception:
            await self.policy_store.mark_tenant_member_deletion_result(
                tenant_id=payload.tenant_id,
                user_id=payload.user_id,
                deletion_intent_key=payload.deletion_intent_key,
                succeeded=False,
                trace_id=ctx.trace_id,
            )
            raise

        await self.policy_store.mark_tenant_member_deletion_result(
            tenant_id=payload.tenant_id,
            user_id=payload.user_id,
            deletion_intent_key=payload.deletion_intent_key,
            succeeded=True,
            trace_id=ctx.trace_id,
        )
