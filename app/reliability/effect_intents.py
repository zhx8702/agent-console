"""Fenced post-commit execution for transactional message effect intents."""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta

from prometheus_client import Counter, Gauge
from sqlalchemy import func, or_, select, update

from app.common.logging import get_logger
from app.common.types import InboundEvent
from app.models.reliability import MessageEffectIntentRow
from app.orchestrator.effect_handlers import (
    EffectHandlerRegistry,
    EffectOwnerExecutionDenied,
)
from app.orchestrator.effects import (
    EFFECT_STATUS_COMPLETED,
    EFFECT_STATUS_FAILED,
    EFFECT_STATUS_PREPARED,
    EFFECT_STATUS_RUNNING,
    EffectCommitRecord,
)
from app.orchestrator.flow import MessageEffect
from app.orchestrator.owner_gate import (
    DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
    OwnerExecutionGate,
    evaluate_owner_execution,
    owner_gate_failure_is_retryable,
)
from app.orchestrator.pipeline import PipelineContext
from app.reliability.message_store import MessageReliabilityStore

log = get_logger(__name__)

EFFECT_INTENTS_COMPLETED = Counter(
    "message_effect_intents_completed_total",
    "Durable message effect intents completed by the relay",
    ["owner", "effect_type"],
)
EFFECT_INTENTS_FAILED = Counter(
    "message_effect_intents_failed_total",
    "Durable message effect intent handler failures",
    ["owner", "effect_type", "terminal"],
)
EFFECT_INTENTS_SKIPPED = Counter(
    "message_effect_intents_owner_skipped_total",
    "Durable message effect intents skipped by the final owner execution gate",
    ["owner", "effect_type"],
)
EFFECT_INTENT_CLAIMS_LOST = Counter(
    "message_effect_intent_claims_lost_total",
    "Effect intent finalizations rejected by a fencing token",
)
EFFECT_INTENT_BACKLOG = Gauge(
    "message_effect_intent_backlog",
    "Prepared, retryable failed, or recoverable running effect intents",
)


class MessageEffectIntentRelay:
    """Execute committed effect intents with lease and attempt fencing.

    Only the transaction that commits the source message can create an
    executable intent.  Handlers receive the stable idempotency key, and a
    stale worker cannot complete or fail an intent after another worker has
    reclaimed it.
    """

    def __init__(
        self,
        store: MessageReliabilityStore,
        registry: EffectHandlerRegistry,
        *,
        worker_id: str,
        poll_interval_seconds: float = 0.5,
        batch_size: int = 32,
        lease_seconds: int = 30,
        handler_timeout_seconds: float = 20.0,
        max_attempts: int = 12,
        owner_gate: OwnerExecutionGate | None = None,
        owner_gate_timeout_seconds: float = DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
    ) -> None:
        base_worker_id = str(worker_id or "effect-intent-relay").strip()
        self._worker_id = f"{base_worker_id[:80]}:{secrets.token_hex(16)}"
        self._store = store
        self._registry = registry
        self._poll_interval = max(0.05, float(poll_interval_seconds))
        self._batch_size = max(1, int(batch_size))
        self._lease_seconds = max(3, int(lease_seconds))
        self._handler_timeout = max(
            0.1,
            min(float(handler_timeout_seconds), self._lease_seconds * 0.8),
        )
        self._max_attempts = max(1, int(max_attempts))
        self._owner_gate = owner_gate
        self._owner_gate_timeout_seconds = owner_gate_timeout_seconds
        self._stop = asyncio.Event()

    async def prepare_worker(self) -> None:
        await self.backlog()

    async def run(self) -> None:
        while not self._stop.is_set():
            completed = await self.drain_once()
            if completed:
                continue
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._poll_interval,
                )
            except TimeoutError:
                continue

    async def stop(self) -> None:
        self._stop.set()

    async def drain_once(self) -> int:
        """Drain a bounded number of intents with a fresh lease per item.

        Claiming an entire batch before serial execution lets later rows sit
        idle until their shared lease expires.  Claim immediately before each
        handler instead, so the configured lease always covers that handler's
        own gate, execution, and fenced finalization window.
        """

        completed = 0
        for _ in range(self._batch_size):
            batch_completed, claimed = await self._drain_claimed_batch(
                claim_limit=1
            )
            completed += batch_completed
            if claimed == 0:
                break
        await self.backlog()
        return completed

    async def _drain_claimed_batch(
        self,
        *,
        claim_limit: int | None = None,
    ) -> tuple[int, int]:
        rows = await self._claim_batch(limit=claim_limit)
        completed = 0
        for row in rows:
            try:
                ctx = self._pipeline_context(row)
                effect = MessageEffect(
                    type=row.effect_type,
                    owner=row.owner,
                    payload=dict(row.payload or {}),
                    idempotency_key=row.idempotency_key,
                    producer_owner=str(row.producer_owner or row.owner),
                )
                record = EffectCommitRecord(
                    type=row.effect_type,
                    owner=row.owner,
                    idempotency_key=row.idempotency_key,
                    producer_owner=str(row.producer_owner or row.owner),
                    payload=dict(row.payload or {}),
                    status=EFFECT_STATUS_RUNNING,
                    dry_run=bool(row.dry_run),
                    tenant_id=row.tenant_id,
                    claim_owner=row.claim_owner,
                    lease_expires_at=(
                        row.claim_until.isoformat() if row.claim_until is not None else ""
                    ),
                    attempt=row.attempts,
                )
                gate_rejected = False
                if row.effect_type == "forget_member" and row.owner in {
                    "core",
                    "memory",
                }:
                    # Privacy erasure is a compensating operation. Legacy
                    # durable rows used owner=memory; treating that narrow
                    # allowlisted identity as core prevents a plugin disable
                    # from permanently discarding a deletion request.
                    gate_owners = ("core",)
                else:
                    gate_owners = tuple(
                        dict.fromkeys(
                            (
                                str(row.producer_owner or row.owner),
                                str(row.owner),
                            )
                        )
                    )
                for gate_owner in gate_owners:
                    decision = await evaluate_owner_execution(
                        self._owner_gate,
                        gate_owner,
                        ctx,
                        timeout_seconds=self._owner_gate_timeout_seconds,
                    )
                    if decision.allowed:
                        continue
                    gate_rejected = True
                    if owner_gate_failure_is_retryable(decision.reason):
                        await self._mark_failed(
                            row,
                            RuntimeError(decision.reason),
                            retryable=True,
                        )
                    elif await self._complete_owner_skipped(
                        row,
                        executing_owner=gate_owner,
                        reason=decision.reason,
                    ):
                        completed += 1
                    break
                if gate_rejected:
                    continue
                handler = self._registry.get(row.effect_type, row.owner)
                if handler is None:
                    await self._mark_failed(
                        row,
                        LookupError(f"missing effect handler: {row.owner}:{row.effect_type}"),
                        retryable=False,
                    )
                    continue
                await asyncio.wait_for(
                    handler(effect, ctx, record),
                    timeout=self._handler_timeout,
                )
            except EffectOwnerExecutionDenied as exc:
                if owner_gate_failure_is_retryable(exc.reason):
                    await self._mark_failed(
                        row,
                        RuntimeError(exc.reason),
                        retryable=True,
                    )
                    continue
                if await self._complete_owner_skipped(
                    row,
                    executing_owner=exc.owner,
                    reason=exc.reason,
                ):
                    completed += 1
                continue
            except Exception as exc:
                await self._mark_failed(row, exc, retryable=True)
                continue
            if await self._mark_completed(row):
                EFFECT_INTENTS_COMPLETED.labels(
                    owner=row.owner,
                    effect_type=row.effect_type,
                ).inc()
                completed += 1
            else:
                EFFECT_INTENT_CLAIMS_LOST.inc()
                log.warning(
                    "message_effect_intent.claim_lost_on_complete",
                    tenant_id=row.tenant_id,
                    idempotency_key=row.idempotency_key,
                    attempt=row.attempts,
                )
        return completed, len(rows)

    async def _complete_owner_skipped(
        self,
        row: MessageEffectIntentRow,
        *,
        executing_owner: str,
        reason: str,
    ) -> bool:
        if not await self._mark_completed(row):
            EFFECT_INTENT_CLAIMS_LOST.inc()
            return False
        EFFECT_INTENTS_SKIPPED.labels(
            owner=str(executing_owner or row.owner),
            effect_type=row.effect_type,
        ).inc()
        log.info(
            "message_effect_intent.owner_skipped",
            tenant_id=row.tenant_id,
            effect_owner=row.owner,
            executing_owner=str(executing_owner or row.owner),
            effect_type=row.effect_type,
            reason=str(reason or "owner_execution_denied")[:64],
            attempt=row.attempts,
        )
        return True

    async def backlog(self) -> int:
        now = datetime.now(UTC)
        async with self._store.session_factory()() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(MessageEffectIntentRow)
                .where(
                    or_(
                        MessageEffectIntentRow.status == EFFECT_STATUS_PREPARED,
                        (
                            (MessageEffectIntentRow.status == EFFECT_STATUS_FAILED)
                            & MessageEffectIntentRow.available_at.is_not(None)
                        ),
                        (
                            (MessageEffectIntentRow.status == EFFECT_STATUS_RUNNING)
                            & (MessageEffectIntentRow.attempts < self._max_attempts)
                            & or_(
                                MessageEffectIntentRow.claim_until.is_(None),
                                MessageEffectIntentRow.claim_until <= now,
                            )
                        ),
                    )
                )
            )
        value = int(count or 0)
        EFFECT_INTENT_BACKLOG.set(value)
        return value

    async def _claim_batch(
        self,
        *,
        limit: int | None = None,
    ) -> list[MessageEffectIntentRow]:
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=self._lease_seconds)
        async with self._store.session_factory()() as db:
            async with db.begin():
                await db.execute(
                    update(MessageEffectIntentRow)
                    .where(
                        MessageEffectIntentRow.status == EFFECT_STATUS_RUNNING,
                        MessageEffectIntentRow.attempts >= self._max_attempts,
                        or_(
                            MessageEffectIntentRow.claim_until.is_(None),
                            MessageEffectIntentRow.claim_until <= now,
                        ),
                    )
                    .values(
                        status=EFFECT_STATUS_FAILED,
                        available_at=None,
                        claim_owner="",
                        claim_token="",
                        claim_until=None,
                        failed_at=now,
                        last_error="effect_claim_expired_after_max_attempts",
                        updated_at=now,
                    )
                )
                stmt = (
                    select(MessageEffectIntentRow)
                    .where(
                        MessageEffectIntentRow.attempts < self._max_attempts,
                        or_(
                            (
                                (MessageEffectIntentRow.status == EFFECT_STATUS_PREPARED)
                                & or_(
                                    MessageEffectIntentRow.available_at.is_(None),
                                    MessageEffectIntentRow.available_at <= now,
                                )
                            ),
                            (
                                (MessageEffectIntentRow.status == EFFECT_STATUS_FAILED)
                                & MessageEffectIntentRow.available_at.is_not(None)
                                & (MessageEffectIntentRow.available_at <= now)
                            ),
                            (
                                (MessageEffectIntentRow.status == EFFECT_STATUS_RUNNING)
                                & or_(
                                    MessageEffectIntentRow.claim_until.is_(None),
                                    MessageEffectIntentRow.claim_until <= now,
                                )
                            ),
                        ),
                    )
                    .order_by(MessageEffectIntentRow.created_at)
                    .limit(
                        self._batch_size
                        if limit is None
                        else max(1, min(int(limit), self._batch_size))
                    )
                    .with_for_update(skip_locked=True)
                )
                rows = list((await db.execute(stmt)).scalars().all())
                for row in rows:
                    row.status = EFFECT_STATUS_RUNNING
                    row.claim_owner = self._worker_id
                    row.claim_token = secrets.token_hex(16)
                    row.claim_until = lease_until
                    row.attempts += 1
                    row.available_at = None
                    row.started_at = now
                    row.failed_at = None
                    row.last_error = ""
            for row in rows:
                db.expunge(row)
        return rows

    async def _mark_completed(self, row: MessageEffectIntentRow) -> bool:
        now = datetime.now(UTC)
        async with self._store.session_factory()() as db:
            async with db.begin():
                result = await db.execute(
                    update(MessageEffectIntentRow)
                    .where(
                        MessageEffectIntentRow.tenant_id == row.tenant_id,
                        MessageEffectIntentRow.idempotency_key == row.idempotency_key,
                        MessageEffectIntentRow.status == EFFECT_STATUS_RUNNING,
                        MessageEffectIntentRow.claim_owner == self._worker_id,
                        MessageEffectIntentRow.claim_token == row.claim_token,
                        MessageEffectIntentRow.attempts == row.attempts,
                        MessageEffectIntentRow.claim_until.is_not(None),
                        MessageEffectIntentRow.claim_until > now,
                    )
                    .values(
                        status=EFFECT_STATUS_COMPLETED,
                        available_at=None,
                        claim_owner="",
                        claim_token="",
                        claim_until=None,
                        completed_at=now,
                        failed_at=None,
                        last_error="",
                        updated_at=now,
                    )
                )
        return int(getattr(result, "rowcount", 0) or 0) == 1

    async def _mark_failed(
        self,
        row: MessageEffectIntentRow,
        exc: Exception,
        *,
        retryable: bool,
    ) -> bool:
        now = datetime.now(UTC)
        terminal = not retryable or row.attempts >= self._max_attempts
        delay = min(300, 2 ** min(row.attempts, 8))
        async with self._store.session_factory()() as db:
            async with db.begin():
                result = await db.execute(
                    update(MessageEffectIntentRow)
                    .where(
                        MessageEffectIntentRow.tenant_id == row.tenant_id,
                        MessageEffectIntentRow.idempotency_key == row.idempotency_key,
                        MessageEffectIntentRow.status == EFFECT_STATUS_RUNNING,
                        MessageEffectIntentRow.claim_owner == self._worker_id,
                        MessageEffectIntentRow.claim_token == row.claim_token,
                        MessageEffectIntentRow.attempts == row.attempts,
                        MessageEffectIntentRow.claim_until.is_not(None),
                        MessageEffectIntentRow.claim_until > now,
                    )
                    .values(
                        status=EFFECT_STATUS_FAILED,
                        available_at=(None if terminal else now + timedelta(seconds=delay)),
                        claim_owner="",
                        claim_token="",
                        claim_until=None,
                        failed_at=now,
                        last_error=f"{exc.__class__.__name__}:{str(exc)[:500]}",
                        updated_at=now,
                    )
                )
        updated = int(getattr(result, "rowcount", 0) or 0) == 1
        if updated:
            EFFECT_INTENTS_FAILED.labels(
                owner=row.owner,
                effect_type=row.effect_type,
                terminal=str(terminal).lower(),
            ).inc()
        else:
            EFFECT_INTENT_CLAIMS_LOST.inc()
        return updated

    @staticmethod
    def _pipeline_context(row: MessageEffectIntentRow) -> PipelineContext:
        raw_context = dict(row.context or {})
        event_payload = raw_context.get("event")
        if not isinstance(event_payload, dict):
            raise ValueError("effect_intent_event_context_missing")
        event = InboundEvent.model_validate(event_payload)
        if event.tenant_id != row.tenant_id or event.session_id != row.session_id:
            raise ValueError("effect_intent_event_scope_mismatch")
        return PipelineContext(event=event, trace_id=row.trace_id or event.trace_id)
