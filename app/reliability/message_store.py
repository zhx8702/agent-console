from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from prometheus_client import Counter, Gauge
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bus.base import BusMessage, IdempotentMessagePublisher, MessageBus
from app.common.logging import get_logger
from app.common.types import InboundEvent
from app.infra.db import get_session_factory
from app.models.reliability import (
    MessageEffectIntentRow,
    MessageOutboxRow,
    ProcessedMessageRow,
)
from app.orchestrator.effects import (
    EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT,
    EFFECT_COMMIT_SEMANTICS_DRY_RUN_ONLY,
    EFFECT_STATUS_COMPLETED,
    EFFECT_STATUS_PREPARED,
    EffectCommitRecord,
    normalize_effect,
)
from app.orchestrator.flow import MessageEffect
from app.orchestrator.outcome import ProcessingOutcome
from app.orchestrator.pipeline import PipelineContext

log = get_logger(__name__)

OUTBOX_ENQUEUED = Counter(
    "message_outbox_enqueued_total",
    "Outbound messages committed to the database outbox",
)
OUTBOX_PUBLISHED = Counter(
    "message_outbox_published_total",
    "Database outbox messages published to the transport",
)
OUTBOX_PUBLISH_FAILURES = Counter(
    "message_outbox_publish_failures_total",
    "Database outbox publication failures",
)
OUTBOX_DEAD_LETTERED = Counter(
    "message_outbox_dead_lettered_total",
    "Outbound messages moved to the durable outbox dead-letter state",
)
OUTBOX_BACKLOG = Gauge(
    "message_outbox_backlog",
    "Pending or recoverable database outbox messages",
)
EFFECT_INTENTS_ENQUEUED = Counter(
    "message_effect_intents_enqueued_total",
    "Flow effect intents committed with the source message transaction",
    ["status"],
)


@dataclass(frozen=True, slots=True)
class MessageClaim:
    claimed: bool
    status: str = ""
    route_label: str = ""
    reason: str = ""
    error_type: str = ""
    claim_token: str = ""


@dataclass(frozen=True, slots=True)
class _OutboxIntent:
    stream: str
    payload: dict[str, Any]
    headers: dict[str, str] | None
    partition_key: str | None


@dataclass(frozen=True, slots=True)
class _EffectIntent:
    tenant_id: str
    idempotency_key: str
    source_message_id: str
    session_id: str
    trace_id: str
    owner: str
    producer_owner: str
    effect_type: str
    payload: dict[str, Any]
    context: dict[str, Any]
    status: str
    dry_run: bool


@dataclass(slots=True)
class _MessageStage:
    outbox: list[_OutboxIntent]
    effects: dict[tuple[str, str], _EffectIntent]


def _lease_expired(value: datetime | None) -> bool:
    if value is None:
        return True
    now = datetime.now(UTC)
    if value.tzinfo is None:
        now = now.replace(tzinfo=None)
    return value <= now


class MessageReliabilityStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._active_db: ContextVar[AsyncSession | None] = ContextVar(
            f"message_reliability_db_{id(self)}",
            default=None,
        )
        self._active_stage: ContextVar[_MessageStage | None] = ContextVar(
            f"message_reliability_stage_{id(self)}",
            default=None,
        )
        self._claim_owner = f"inbox:{secrets.token_hex(24)}"

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or get_session_factory()

    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Return the configured factory for relay-owned short transactions."""

        return self._factory()

    @contextmanager
    def bind(self, db: AsyncSession) -> Iterator[None]:
        token = self._active_db.set(db)
        try:
            yield
        finally:
            self._active_db.reset(token)

    @contextmanager
    def stage(self) -> Iterator[None]:
        existing = self._active_stage.get()
        if existing is not None:
            yield
            return
        token = self._active_stage.set(_MessageStage(outbox=[], effects={}))
        try:
            yield
        finally:
            self._active_stage.reset(token)

    @property
    def active_db(self) -> AsyncSession | None:
        return self._active_db.get()

    @property
    def stage_active(self) -> bool:
        return self._active_stage.get() is not None

    async def acquire(
        self,
        event: InboundEvent,
        *,
        lease_seconds: float,
    ) -> MessageClaim:
        """Acquire a committed, recoverable inbox processing lease."""

        claim_token = secrets.token_hex(16)
        claim_until = datetime.now(UTC) + timedelta(
            seconds=max(1.0, float(lease_seconds)),
        )
        async with self._factory()() as db:
            async with db.begin():
                return await self.claim(
                    db,
                    event,
                    claim_owner=self._claim_owner,
                    claim_token=claim_token,
                    claim_until=claim_until,
                )

    async def claim(
        self,
        db: AsyncSession,
        event: InboundEvent,
        *,
        claim_owner: str = "",
        claim_token: str = "",
        claim_until: datetime | None = None,
    ) -> MessageClaim:
        values = {
            "tenant_id": event.tenant_id,
            "message_id": event.message_id,
            "session_id": event.session_id,
            "user_id": event.user_id,
            "trace_id": event.trace_id,
            "status": "processing",
            "received_at": event.received_at,
            "claim_owner": claim_owner,
            "claim_token": claim_token,
            "claim_until": claim_until,
            "attempts": 1,
        }
        dialect = db.bind.dialect.name if db.bind is not None else ""
        inserted = False
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert

            result = await db.execute(
                insert(ProcessedMessageRow)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "message_id"],
                )
                .returning(ProcessedMessageRow.message_id)
            )
            inserted = result.scalar_one_or_none() is not None
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert

            result = await db.execute(
                insert(ProcessedMessageRow)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "message_id"],
                )
                .returning(ProcessedMessageRow.message_id)
            )
            inserted = result.scalar_one_or_none() is not None
        else:
            existing = await db.get(
                ProcessedMessageRow,
                {
                    "tenant_id": event.tenant_id,
                    "message_id": event.message_id,
                },
            )
            if existing is None:
                db.add(ProcessedMessageRow(**values))
                await db.flush()
                inserted = True

        if inserted:
            return MessageClaim(
                claimed=True,
                status="processing",
                claim_token=claim_token,
            )

        if claim_owner and claim_token:
            now = datetime.now(UTC)
            reclaimed = await db.execute(
                update(ProcessedMessageRow)
                .where(
                    ProcessedMessageRow.tenant_id == event.tenant_id,
                    ProcessedMessageRow.message_id == event.message_id,
                    ProcessedMessageRow.status == "processing",
                    or_(
                        ProcessedMessageRow.claim_until.is_(None),
                        ProcessedMessageRow.claim_until <= now,
                    ),
                )
                .values(
                    claim_owner=claim_owner,
                    claim_token=claim_token,
                    claim_until=claim_until,
                    attempts=ProcessedMessageRow.attempts + 1,
                    route_label="",
                    reason="",
                    error_type="",
                )
                .returning(ProcessedMessageRow.message_id)
            )
            if reclaimed.scalar_one_or_none() is not None:
                return MessageClaim(
                    claimed=True,
                    status="processing",
                    claim_token=claim_token,
                )

        row = await db.get(
            ProcessedMessageRow,
            {
                "tenant_id": event.tenant_id,
                "message_id": event.message_id,
            },
        )
        if row is None:
            # A concurrent transaction can still be finishing after the
            # conflict. Re-reading once after yielding lets READ COMMITTED see
            # its terminal row without running the business pipeline twice.
            await asyncio.sleep(0)
            row = await db.get(
                ProcessedMessageRow,
                {
                    "tenant_id": event.tenant_id,
                    "message_id": event.message_id,
                },
                populate_existing=True,
            )
        if row is None:
            raise RuntimeError("processed_message_claim_conflict_without_row")

        return MessageClaim(
            claimed=False,
            status=row.status,
            route_label=row.route_label,
            reason=row.reason,
            error_type=row.error_type,
        )

    async def release(
        self,
        event: InboundEvent,
        *,
        claim_token: str,
    ) -> bool:
        if not claim_token:
            return False
        async with self._factory()() as db:
            async with db.begin():
                result = await db.execute(
                    delete(ProcessedMessageRow).where(
                        ProcessedMessageRow.tenant_id == event.tenant_id,
                        ProcessedMessageRow.message_id == event.message_id,
                        ProcessedMessageRow.status == "processing",
                        ProcessedMessageRow.claim_owner == self._claim_owner,
                        ProcessedMessageRow.claim_token == claim_token,
                    )
                )
        return int(getattr(result, "rowcount", 0) or 0) == 1

    async def complete(
        self,
        db: AsyncSession,
        event: InboundEvent,
        outcome: ProcessingOutcome,
        *,
        claim_token: str = "",
    ) -> None:
        row = await db.get(
            ProcessedMessageRow,
            {
                "tenant_id": event.tenant_id,
                "message_id": event.message_id,
            },
            populate_existing=True,
        )
        if row is None or row.status != "processing":
            raise RuntimeError(
                "processed_message_completion_lost:"
                f"{'missing' if row is None else row.status}"
            )
        if claim_token and (
            row.claim_owner != self._claim_owner
            or row.claim_token != claim_token
            or _lease_expired(row.claim_until)
        ):
            raise RuntimeError("processed_message_completion_lease_lost")
        row.status = outcome.status.value
        row.route_label = outcome.route_label
        row.reason = outcome.reason
        row.error_type = outcome.error_type
        row.completed_at = datetime.now(UTC)
        row.claim_owner = ""
        row.claim_token = ""
        row.claim_until = None

    async def flush_stage(self, db: AsyncSession) -> None:
        stage = self._active_stage.get()
        if stage is None:
            raise RuntimeError("message_stage_not_active")
        for intent in stage.outbox:
            await self.enqueue(
                db,
                stream=intent.stream,
                payload=intent.payload,
                headers=intent.headers,
                partition_key=intent.partition_key,
            )
        for intent in stage.effects.values():
            await self._insert_effect_intent(db, intent)

    async def stage_effect(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        *,
        sequence: int = 0,
        deferred: bool = False,
        dry_run: bool = False,
    ) -> EffectCommitRecord:
        """Stage an effect intent for the source message's final transaction.

        Effects whose business write already happened through the ambient
        session/outbox stage are committed as ``completed``.  Effects that
        still need a handler are committed as ``prepared`` for the fenced
        post-commit relay.
        """

        stage = self._active_stage.get()
        if stage is None:
            raise RuntimeError("message_stage_not_active")
        normalized = normalize_effect(effect, ctx, sequence=sequence)
        tenant_id = str(ctx.event.tenant_id or "").strip()
        if not tenant_id:
            raise ValueError("effect_intent_tenant_required")
        payload = dict(normalized.payload)
        semantics = str(payload.get("commit_semantics") or "").strip()
        is_dry_run = bool(dry_run or semantics == EFFECT_COMMIT_SEMANTICS_DRY_RUN_ONLY)
        status = (
            EFFECT_STATUS_COMPLETED
            if is_dry_run
            or semantics == EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT
            or bool(payload.get("side_effects_executed_before_commit"))
            or not deferred
            else EFFECT_STATUS_PREPARED
        )
        intent = _EffectIntent(
            tenant_id=tenant_id,
            idempotency_key=normalized.idempotency_key,
            source_message_id=str(ctx.event.message_id or "").strip(),
            session_id=str(ctx.event.session_id or "").strip(),
            trace_id=str(ctx.event.trace_id or ctx.trace_id or "").strip(),
            owner=normalized.owner,
            producer_owner=(
                str(normalized.producer_owner or "").strip() or normalized.owner
            ),
            effect_type=normalized.type,
            payload=payload,
            context={"event": ctx.event.model_dump(mode="json")},
            status=status,
            dry_run=is_dry_run,
        )
        identity = (intent.tenant_id, intent.idempotency_key)
        existing = stage.effects.get(identity)
        if existing is not None:
            if existing != intent:
                raise RuntimeError("effect_intent_idempotency_conflict")
            return EffectCommitRecord(
                type=existing.effect_type,
                owner=existing.owner,
                idempotency_key=existing.idempotency_key,
                producer_owner=existing.producer_owner,
                payload=dict(existing.payload),
                status=existing.status,
                dry_run=existing.dry_run,
                tenant_id=existing.tenant_id,
            )
        stage.effects[identity] = intent
        return EffectCommitRecord(
            type=intent.effect_type,
            owner=intent.owner,
            idempotency_key=intent.idempotency_key,
            producer_owner=intent.producer_owner,
            payload=dict(intent.payload),
            status=intent.status,
            dry_run=intent.dry_run,
            tenant_id=intent.tenant_id,
        )

    async def enqueue_effect_intent(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        session_id: str,
        source_message_id: str,
        trace_id: str,
        owner: str,
        producer_owner: str = "",
        effect_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        channel: str = "wechat",
        user_id: str = "system",
        context: dict[str, Any] | None = None,
        completed: bool = False,
        dry_run: bool = False,
    ) -> EffectCommitRecord:
        """Insert an effect intent into a caller-owned database transaction.

        Admin aggregate APIs use this entrypoint after applying their config
        mutations to ``db``.  They must not commit here: the caller's commit
        atomically publishes both the configuration and its SDK-side intent.
        """

        event = InboundEvent.model_validate(
            {
                "tenant_id": tenant_id,
                "channel": channel,
                "message_id": source_message_id,
                "session_id": session_id,
                "user_id": user_id or "system",
                "message": {"type": "text", "content": ""},
                "trace_id": trace_id,
                "metadata": {"effect_intent_source": "admin_transaction"},
            }
        )
        normalized_owner = str(owner or "").strip()
        normalized_type = str(effect_type or "").strip()
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_owner or not normalized_type or not normalized_key:
            raise ValueError("effect_intent_identity_required")
        status = EFFECT_STATUS_COMPLETED if completed else EFFECT_STATUS_PREPARED
        intent = _EffectIntent(
            tenant_id=event.tenant_id,
            idempotency_key=normalized_key,
            source_message_id=event.message_id,
            session_id=event.session_id,
            trace_id=event.trace_id,
            owner=normalized_owner,
            producer_owner=str(producer_owner or "").strip() or normalized_owner,
            effect_type=normalized_type,
            payload=dict(payload),
            context={"event": event.model_dump(mode="json"), **dict(context or {})},
            status=status,
            dry_run=bool(dry_run),
        )
        return await self._insert_effect_intent(db, intent)

    async def _insert_effect_intent(
        self,
        db: AsyncSession,
        intent: _EffectIntent,
    ) -> EffectCommitRecord:
        identity = {
            "tenant_id": intent.tenant_id,
            "idempotency_key": intent.idempotency_key,
        }
        existing = await db.get(MessageEffectIntentRow, identity)
        if existing is not None:
            if (
                existing.session_id != intent.session_id
                or existing.owner != intent.owner
                or existing.producer_owner != intent.producer_owner
                or existing.effect_type != intent.effect_type
                or existing.payload != intent.payload
                or bool(existing.dry_run) != intent.dry_run
            ):
                raise RuntimeError("effect_intent_idempotency_conflict")
            return EffectCommitRecord(
                type=existing.effect_type,
                owner=existing.owner,
                idempotency_key=existing.idempotency_key,
                producer_owner=existing.producer_owner,
                payload=dict(existing.payload or {}),
                status=existing.status,
                dry_run=bool(existing.dry_run),
                tenant_id=existing.tenant_id,
                claim_owner=existing.claim_owner,
                lease_expires_at=(
                    existing.claim_until.isoformat()
                    if existing.claim_until is not None
                    else ""
                ),
                attempt=existing.attempts,
            )
        now = datetime.now(UTC)
        db.add(
            MessageEffectIntentRow(
                tenant_id=intent.tenant_id,
                idempotency_key=intent.idempotency_key,
                source_message_id=intent.source_message_id,
                session_id=intent.session_id,
                trace_id=intent.trace_id,
                owner=intent.owner,
                producer_owner=intent.producer_owner,
                effect_type=intent.effect_type,
                payload=dict(intent.payload),
                context=dict(intent.context),
                status=intent.status,
                dry_run=intent.dry_run,
                available_at=now if intent.status == EFFECT_STATUS_PREPARED else None,
                completed_at=now if intent.status == EFFECT_STATUS_COMPLETED else None,
            )
        )
        EFFECT_INTENTS_ENQUEUED.labels(status=intent.status).inc()
        return EffectCommitRecord(
            type=intent.effect_type,
            owner=intent.owner,
            idempotency_key=intent.idempotency_key,
            producer_owner=intent.producer_owner,
            payload=dict(intent.payload),
            status=intent.status,
            dry_run=intent.dry_run,
            tenant_id=intent.tenant_id,
        )

    async def enqueue(
        self,
        db: AsyncSession,
        *,
        stream: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None,
        partition_key: str | None,
    ) -> str:
        reply_id = str(payload.get("reply_id") or "").strip()
        tenant_id = str(payload.get("tenant_id") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        if not reply_id or not tenant_id or not session_id:
            raise ValueError("outbox_reply_scope_required")
        identity = {"tenant_id": tenant_id, "reply_id": reply_id}
        existing = await db.get(MessageOutboxRow, identity)
        if existing is not None:
            if (
                existing.tenant_id != tenant_id
                or existing.session_id != session_id
                or existing.payload != payload
            ):
                raise RuntimeError("outbox_reply_id_conflict")
            return f"outbox:{reply_id}"

        db.add(
            MessageOutboxRow(
                reply_id=reply_id,
                tenant_id=tenant_id,
                session_id=session_id,
                trace_id=str(payload.get("trace_id") or ""),
                stream=stream,
                partition_key=partition_key or f"{tenant_id}:{session_id}",
                payload=dict(payload),
                headers=dict(headers or {}),
                status="pending",
                available_at=datetime.now(UTC),
            )
        )
        OUTBOX_ENQUEUED.inc()
        return f"outbox:{reply_id}"

    async def backlog(self) -> int:
        async with self._factory()() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(MessageOutboxRow)
                .where(MessageOutboxRow.status.in_(("pending", "publishing")))
            )
        value = int(count or 0)
        OUTBOX_BACKLOG.set(value)
        return value


class TransactionalOutboxBus:
    """MessageBus adapter that writes outbound messages into the ambient DB."""

    def __init__(
        self,
        delegate: MessageBus,
        store: MessageReliabilityStore,
        *,
        outbound_stream: str,
    ) -> None:
        self._delegate = delegate
        self._store = store
        self._outbound_stream = outbound_stream

    async def publish(
        self,
        stream: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        partition_key: str | None = None,
    ) -> str:
        db = self._store.active_db
        if db is not None and stream == self._outbound_stream:
            return await self._store.enqueue(
                db,
                stream=stream,
                payload=payload,
                headers=headers,
                partition_key=partition_key,
            )
        stage = self._store._active_stage.get()
        if stage is not None and stream == self._outbound_stream:
            stage.outbox.append(
                _OutboxIntent(
                    stream=stream,
                    payload=dict(payload),
                    headers=dict(headers) if headers is not None else None,
                    partition_key=partition_key,
                )
            )
            reply_id = str(payload.get("reply_id") or "").strip()
            return f"staged:{reply_id or len(stage.outbox)}"
        return await self._delegate.publish(
            stream,
            payload,
            headers=headers,
            partition_key=partition_key,
        )

    async def ensure_group(self, stream: str, group: str) -> None:
        await self._delegate.ensure_group(stream, group)

    def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler: Callable[[BusMessage], Awaitable[None]],
        *,
        batch_size: int = 16,
        block_ms: int = 5_000,
    ) -> AsyncIterator[None]:
        return self._delegate.consume(
            stream,
            group,
            consumer,
            handler,
            batch_size=batch_size,
            block_ms=block_ms,
        )

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        await self._delegate.ack(stream, group, message_id)

    async def move_to_dlq(self, message: BusMessage, reason: str) -> None:
        await self._delegate.move_to_dlq(message, reason)

    async def close(self) -> None:
        await self._delegate.close()


class MessageOutboxRelay:
    def __init__(
        self,
        store: MessageReliabilityStore,
        bus: MessageBus,
        *,
        worker_id: str,
        poll_interval_seconds: float = 1.0,
        batch_size: int = 50,
        lease_seconds: int = 30,
        publish_timeout_seconds: float = 10.0,
        max_attempts: int = 12,
    ) -> None:
        self._store = store
        self._bus = bus
        base_worker_id = str(worker_id or "outbox-relay").strip() or "outbox-relay"
        self._worker_id = f"{base_worker_id[:80]}:{secrets.token_hex(16)}"
        self._poll_interval = max(0.05, float(poll_interval_seconds))
        self._batch_size = max(1, int(batch_size))
        self._lease_seconds = max(3, int(lease_seconds))
        self._publish_timeout = max(
            0.1,
            min(
                float(publish_timeout_seconds),
                self._lease_seconds * 0.8,
            ),
        )
        self._max_attempts = max(1, int(max_attempts))
        self._stop = asyncio.Event()

    async def prepare_worker(self) -> None:
        """Probe the outbox database/schema before advertising readiness."""
        await self._store.backlog()

    async def run(self) -> None:
        while not self._stop.is_set():
            published = await self.drain_once()
            if published:
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
        rows = await self._claim_batch()
        published = 0
        for row in rows:
            if not await self._renew_claim(row):
                log.warning(
                    "message_outbox.claim_lost_before_publish",
                    tenant_id=row.tenant_id,
                    reply_id=row.reply_id,
                )
                continue
            try:
                if not isinstance(self._bus, IdempotentMessagePublisher):
                    raise RuntimeError("outbox_transport_idempotency_required")
                message_id = await asyncio.wait_for(
                    self._bus.publish_once(
                        row.stream,
                        dict(row.payload),
                        idempotency_key=f"{row.tenant_id}:{row.reply_id}",
                        headers={
                            **dict(row.headers or {}),
                            "outbox_reply_id": row.reply_id,
                            "tenant_id": row.tenant_id,
                            "session_id": row.session_id,
                            "trace_id": row.trace_id,
                        },
                        partition_key=row.partition_key,
                    ),
                    timeout=self._publish_timeout,
                )
            except Exception as exc:
                await self._mark_failed(
                    row.tenant_id,
                    row.reply_id,
                    row.lease_token,
                    exc,
                )
                OUTBOX_PUBLISH_FAILURES.inc()
                continue
            try:
                await self._mark_published(
                    row.tenant_id,
                    row.reply_id,
                    row.lease_token,
                    message_id,
                )
            except Exception as exc:
                # The transport may already contain the reply. Leave the
                # leased row recoverable; downstream receives reply_id as an
                # idempotency key, so a replay remains safe.
                log.exception(
                    "message_outbox.mark_published_failed",
                    tenant_id=row.tenant_id,
                    reply_id=row.reply_id,
                    error_type=exc.__class__.__name__,
                )
                OUTBOX_PUBLISH_FAILURES.inc()
                continue
            OUTBOX_PUBLISHED.inc()
            published += 1
        await self._store.backlog()
        return published

    async def _claim_batch(self) -> list[MessageOutboxRow]:
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=self._lease_seconds)
        async with self._store.session_factory()() as db:
            async with db.begin():
                stmt = (
                    select(MessageOutboxRow)
                    .where(
                        MessageOutboxRow.available_at <= now,
                        or_(
                            MessageOutboxRow.status == "pending",
                            (
                                (MessageOutboxRow.status == "publishing")
                                & (
                                    MessageOutboxRow.lease_until.is_(None)
                                    | (MessageOutboxRow.lease_until < now)
                                )
                            ),
                        ),
                    )
                    .order_by(MessageOutboxRow.created_at)
                    .limit(self._batch_size)
                    .with_for_update(skip_locked=True)
                )
                rows = list((await db.execute(stmt)).scalars().all())
                for row in rows:
                    claim_token = secrets.token_hex(16)
                    row.status = "publishing"
                    row.lease_owner = self._worker_id
                    row.lease_token = claim_token
                    row.lease_until = lease_until
                    row.attempts += 1
            for row in rows:
                db.expunge(row)
        return rows

    async def _renew_claim(self, row: MessageOutboxRow) -> bool:
        lease_until = datetime.now(UTC) + timedelta(seconds=self._lease_seconds)
        async with self._store.session_factory()() as db:
            async with db.begin():
                result = await db.execute(
                    update(MessageOutboxRow)
                    .where(
                        MessageOutboxRow.tenant_id == row.tenant_id,
                        MessageOutboxRow.reply_id == row.reply_id,
                        MessageOutboxRow.status == "publishing",
                        MessageOutboxRow.lease_owner == self._worker_id,
                        MessageOutboxRow.lease_token == row.lease_token,
                    )
                    .values(lease_until=lease_until)
                )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            return False
        row.lease_until = lease_until
        return True

    async def _mark_published(
        self,
        tenant_id: str,
        reply_id: str,
        lease_token: str,
        message_id: str,
    ) -> None:
        async with self._store.session_factory()() as db:
            async with db.begin():
                result = await db.execute(
                    update(MessageOutboxRow)
                    .where(
                        MessageOutboxRow.tenant_id == tenant_id,
                        MessageOutboxRow.reply_id == reply_id,
                        MessageOutboxRow.status == "publishing",
                        MessageOutboxRow.lease_owner == self._worker_id,
                        MessageOutboxRow.lease_token == lease_token,
                    )
                    .values(
                        status="published",
                        published_message_id=message_id,
                        published_at=datetime.now(UTC),
                        lease_owner="",
                        lease_token="",
                        lease_until=None,
                        last_error="",
                    )
                )
                if int(getattr(result, "rowcount", 0) or 0) != 1:
                    raise RuntimeError("outbox_publish_lease_lost")

    async def _mark_failed(
        self,
        tenant_id: str,
        reply_id: str,
        lease_token: str,
        exc: Exception,
    ) -> None:
        async with self._store.session_factory()() as db:
            async with db.begin():
                row = await db.get(
                    MessageOutboxRow,
                    {"tenant_id": tenant_id, "reply_id": reply_id},
                )
                if (
                    row is None
                    or row.status != "publishing"
                    or row.lease_owner != self._worker_id
                    or row.lease_token != lease_token
                ):
                    return
                delay = min(300, 2 ** min(row.attempts, 8))
                now = datetime.now(UTC)
                if row.attempts >= self._max_attempts:
                    row.status = "dead_letter"
                    row.dead_lettered_at = now
                    OUTBOX_DEAD_LETTERED.inc()
                else:
                    row.status = "pending"
                    row.available_at = now + timedelta(seconds=delay)
                row.lease_owner = ""
                row.lease_token = ""
                row.lease_until = None
                row.last_error = f"{exc.__class__.__name__}:{str(exc)[:500]}"
