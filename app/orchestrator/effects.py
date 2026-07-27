"""Effect commit primitives for message flows.

This module is intentionally small. Existing plugins still perform many
side effects inline; the first step toward moving them behind ``MessageEffect``
is a shared commit log surface with stable idempotency keys.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from urllib.parse import quote

from app.orchestrator.flow import MessageEffect
from app.orchestrator.pipeline import PipelineContext

EFFECT_STATUS_RECORDED = "recorded"
EFFECT_STATUS_DRY_RUN = "dry_run"
EFFECT_STATUS_DUPLICATE = "duplicate"
EFFECT_STATUS_PREPARED = "prepared"
EFFECT_STATUS_RUNNING = "running"
EFFECT_STATUS_COMPLETED = "completed"
EFFECT_STATUS_FAILED = "failed"

EFFECT_LIFECYCLE_STATUSES = frozenset(
    {
        EFFECT_STATUS_PREPARED,
        EFFECT_STATUS_RUNNING,
        EFFECT_STATUS_COMPLETED,
        EFFECT_STATUS_FAILED,
    }
)

EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT = "audit_after_side_effect"
EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT = "gate_before_side_effect"
EFFECT_COMMIT_SEMANTICS_DRY_RUN_ONLY = "dry_run_only"


@dataclass(frozen=True)
class EffectCommitRecord:
    type: str
    owner: str
    idempotency_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = EFFECT_STATUS_RECORDED
    error: str = ""
    dry_run: bool = False
    tenant_id: str = ""
    claim_owner: str = ""
    lease_expires_at: str = ""
    attempt: int = 0
    producer_owner: str = ""


class EffectClaimUnavailable(RuntimeError):
    """Raised when an unexpired execution claim already owns an effect."""

    def __init__(self, record: EffectCommitRecord | None = None) -> None:
        super().__init__("effect_claim_unavailable")
        self.record = record


class EffectClaimLost(RuntimeError):
    """Raised when a stale worker attempts to finalize a reclaimed effect."""

    def __init__(self, record: EffectCommitRecord | None = None) -> None:
        super().__init__("effect_claim_lost")
        self.record = record


class EffectIdempotencyConflictError(RuntimeError):
    """Raised when one effect key is reused for a different command."""

    def __init__(self, reason: str = "effect_idempotency_conflict") -> None:
        super().__init__(reason)
        self.reason = reason


class EffectCommitter(Protocol):
    async def commit(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        *,
        sequence: int = 0,
        dry_run: bool = False,
    ) -> EffectCommitRecord: ...


class EffectAuditLog(Protocol):
    async def claim(
        self,
        *,
        idempotency_key: str,
        tenant_id: str,
        session_id: str,
        trace_id: str,
        owner: str,
        type: str,
        claim_owner: str,
        lease_seconds: int,
        payload: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> EffectCommitRecord: ...

    async def complete(
        self,
        *,
        idempotency_key: str,
        tenant_id: str,
        claim_owner: str,
        attempt: int,
    ) -> EffectCommitRecord: ...

    async def fail(
        self,
        *,
        idempotency_key: str,
        tenant_id: str,
        claim_owner: str,
        attempt: int,
        error: str,
    ) -> EffectCommitRecord: ...

    async def record(
        self,
        *,
        idempotency_key: str,
        tenant_id: str,
        session_id: str,
        trace_id: str,
        owner: str,
        type: str,
        payload: dict[str, Any] | None = None,
        status: str | None = None,
        dry_run: bool = False,
    ) -> EffectCommitRecord: ...


class InMemoryEffectCommitter:
    """Record effects with idempotency semantics, without external side effects."""

    def __init__(self) -> None:
        self.records: list[EffectCommitRecord] = []
        self._seen: dict[tuple[str, str, bool], EffectCommitRecord] = {}

    async def commit(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        *,
        sequence: int = 0,
        dry_run: bool = False,
    ) -> EffectCommitRecord:
        normalized = normalize_effect(effect, ctx, sequence=sequence)
        seen_key = effect_identity(
            ctx.event.tenant_id,
            normalized.idempotency_key,
            dry_run=dry_run,
        )
        existing = self._seen.get(seen_key)
        if existing is not None:
            _assert_same_effect_request(
                existing_type=existing.type,
                existing_owner=existing.owner,
                existing_producer_owner=existing.producer_owner,
                existing_payload=existing.payload,
                requested=normalized,
            )
            return EffectCommitRecord(
                type=existing.type,
                owner=existing.owner,
                idempotency_key=existing.idempotency_key,
                producer_owner=existing.producer_owner,
                payload=dict(existing.payload),
                status=EFFECT_STATUS_DUPLICATE,
                dry_run=dry_run,
                tenant_id=seen_key[0],
            )

        record = EffectCommitRecord(
            type=normalized.type,
            owner=normalized.owner,
            idempotency_key=normalized.idempotency_key,
            producer_owner=normalized.producer_owner,
            payload=dict(normalized.payload),
            status=EFFECT_STATUS_DRY_RUN if dry_run else EFFECT_STATUS_RECORDED,
            dry_run=dry_run,
            tenant_id=seen_key[0],
        )
        self._seen[seen_key] = record
        self.records.append(record)
        return record


class RedisEffectCommitter:
    """Redis-backed idempotency gate for flow effects.

    Real commits and dry-run commits deliberately use separate key namespaces so
    shadow/dry-run executions cannot consume the idempotency key needed by a
    later production execution.
    """

    def __init__(
        self,
        redis: Any,
        *,
        key_prefix: str = "cs:flow:effect",
        ttl_seconds: int = 604_800,
        log_stream: str | None = "cs:flow:effects",
    ) -> None:
        self._redis = redis
        self._key_prefix = str(key_prefix or "cs:flow:effect").rstrip(":")
        self._ttl_seconds = max(1, int(ttl_seconds or 604_800))
        self._log_stream = str(log_stream or "").strip() or None

    async def commit(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        *,
        sequence: int = 0,
        dry_run: bool = False,
    ) -> EffectCommitRecord:
        normalized = normalize_effect(effect, ctx, sequence=sequence)
        identity = effect_identity(
            ctx.event.tenant_id,
            normalized.idempotency_key,
            dry_run=dry_run,
        )
        status = EFFECT_STATUS_DRY_RUN if dry_run else EFFECT_STATUS_RECORDED
        record = EffectCommitRecord(
            type=normalized.type,
            owner=normalized.owner,
            idempotency_key=normalized.idempotency_key,
            producer_owner=normalized.producer_owner,
            payload=dict(normalized.payload),
            status=status,
            dry_run=dry_run,
            tenant_id=identity[0],
        )
        envelope = _record_envelope(record, ctx, sequence=sequence)
        key = self._record_key(
            record.tenant_id,
            record.idempotency_key,
            dry_run=dry_run,
        )
        inserted = await self._redis.set(
            key,
            _json_dumps(envelope),
            nx=True,
            ex=self._ttl_seconds,
        )
        if not inserted:
            return await self._duplicate_record(
                key,
                normalized=normalized,
                dry_run=dry_run,
                tenant_id=identity[0],
            )
        await self._append_log(envelope)
        return record

    def _record_key(
        self,
        tenant_id: str,
        idempotency_key: str,
        *,
        dry_run: bool,
    ) -> str:
        tenant, key, _ = effect_identity(tenant_id, idempotency_key, dry_run=dry_run)
        namespace = "dryrun" if dry_run else "commit"
        return f"{self._key_prefix}:{namespace}:{_redis_key_part(tenant)}:{_redis_key_part(key)}"

    async def _duplicate_record(
        self,
        key: str,
        *,
        normalized: MessageEffect,
        dry_run: bool,
        tenant_id: str,
    ) -> EffectCommitRecord:
        existing = await self._redis.get(key)
        if isinstance(existing, bytes):
            existing = existing.decode("utf-8")
        if not existing:
            raise EffectIdempotencyConflictError("effect_idempotency_record_unavailable")
        try:
            raw = json.loads(str(existing))
        except (TypeError, ValueError) as exc:
            raise EffectIdempotencyConflictError("effect_idempotency_record_corrupt") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("payload"), dict):
            raise EffectIdempotencyConflictError("effect_idempotency_record_corrupt")
        payload = dict(raw["payload"])
        _assert_same_effect_request(
            existing_type=str(raw.get("type") or ""),
            existing_owner=str(raw.get("owner") or ""),
            existing_producer_owner=str(raw.get("producer_owner") or ""),
            existing_payload=payload,
            requested=normalized,
        )
        return EffectCommitRecord(
            type=normalized.type,
            owner=normalized.owner,
            idempotency_key=normalized.idempotency_key,
            producer_owner=normalized.producer_owner,
            payload=payload,
            status=EFFECT_STATUS_DUPLICATE,
            dry_run=dry_run,
            tenant_id=tenant_id,
        )

    async def _append_log(self, envelope: dict[str, Any]) -> None:
        if self._log_stream is None:
            return
        await self._redis.xadd(
            self._log_stream,
            {"record": _json_dumps(envelope)},
        )


class AuditedEffectCommitter:
    """Use the durable audit log as the execution claim state machine.

    Older ``EffectAuditLog`` implementations that only expose ``record`` keep
    their historical gate-then-audit behavior.  The SQL implementation exposes
    ``claim``/``complete``/``fail`` and becomes authoritative: a handler may run
    only after it has atomically claimed ``running`` and only ``completed`` is
    returned as ``duplicate`` on a later attempt.
    """

    def __init__(
        self,
        committer: EffectCommitter,
        audit_log: EffectAuditLog,
        *,
        fail_closed: bool = True,
        claim_owner: str | None = None,
        lease_seconds: int = 60,
    ) -> None:
        self._committer = committer
        self._audit_log = audit_log
        self._fail_closed = bool(fail_closed)
        self._claim_owner = str(claim_owner or f"effect-worker-{secrets.token_hex(12)}")
        self._lease_seconds = max(1, int(lease_seconds or 60))
        self._active_claims: dict[tuple[str, str, bool], EffectCommitRecord] = {}

    async def commit(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        *,
        sequence: int = 0,
        dry_run: bool = False,
    ) -> EffectCommitRecord:
        claim = getattr(self._audit_log, "claim", None)
        if callable(claim):
            normalized = normalize_effect(effect, ctx, sequence=sequence)
            identity = effect_identity(
                ctx.event.tenant_id,
                normalized.idempotency_key,
                dry_run=dry_run,
            )
            try:
                record = cast(
                    EffectCommitRecord,
                    await claim(
                        idempotency_key=normalized.idempotency_key,
                        tenant_id=ctx.event.tenant_id,
                        session_id=ctx.event.session_id,
                        trace_id=ctx.event.trace_id or ctx.trace_id,
                        owner=normalized.owner,
                        type=normalized.type,
                        claim_owner=self._claim_owner,
                        lease_seconds=self._lease_seconds,
                        payload=dict(normalized.payload),
                        dry_run=dry_run,
                    ),
                )
                record = _record_with_tenant(record, identity[0])
                record = _record_with_producer_owner(
                    record,
                    normalized.producer_owner,
                )
                if record.status == EFFECT_STATUS_RUNNING:
                    self._active_claims[identity] = record
                return record
            except EffectClaimUnavailable:
                raise
            except Exception as exc:
                error = _effect_log_error(exc)
                if self._fail_closed:
                    raise RuntimeError(error) from exc
                fallback = await self._committer.commit(
                    normalized,
                    ctx,
                    sequence=sequence,
                    dry_run=dry_run,
                )
                return _record_with_error(fallback, error)

        return await self._legacy_commit(
            effect,
            ctx,
            sequence=sequence,
            dry_run=dry_run,
        )

    async def mark_completed(self, record: EffectCommitRecord) -> EffectCommitRecord:
        """CAS a running claim to completed after its handler succeeds."""

        identity, record = self._resolve_active_claim(record)
        if record.status != EFFECT_STATUS_RUNNING:
            return record
        complete = getattr(self._audit_log, "complete", None)
        if not callable(complete):
            return record
        try:
            completed = cast(
                EffectCommitRecord,
                await complete(
                    idempotency_key=record.idempotency_key,
                    tenant_id=identity[0],
                    claim_owner=record.claim_owner,
                    attempt=record.attempt,
                ),
            )
            completed = _record_with_producer_owner(
                completed,
                record.producer_owner,
            )
            self._active_claims.pop(identity, None)
            return completed
        except (EffectClaimLost, EffectClaimUnavailable):
            raise
        except Exception as exc:
            error = _effect_log_error(exc)
            if self._fail_closed:
                raise RuntimeError(error) from exc
            return _record_with_error(record, error)

    async def mark_failed(
        self,
        record: EffectCommitRecord,
        *,
        error: str,
    ) -> EffectCommitRecord:
        """CAS a running claim to failed so a later attempt may retry it."""

        identity, record = self._resolve_active_claim(record)
        if record.status != EFFECT_STATUS_RUNNING:
            return record
        fail = getattr(self._audit_log, "fail", None)
        if not callable(fail):
            return record
        try:
            failed = cast(
                EffectCommitRecord,
                await fail(
                    idempotency_key=record.idempotency_key,
                    tenant_id=identity[0],
                    claim_owner=record.claim_owner,
                    attempt=record.attempt,
                    error=str(error or "effect_handler_failed"),
                ),
            )
            failed = _record_with_producer_owner(
                failed,
                record.producer_owner,
            )
            self._active_claims.pop(identity, None)
            return failed
        except (EffectClaimLost, EffectClaimUnavailable):
            raise
        except Exception as exc:
            log_error = _effect_log_error(exc)
            if self._fail_closed:
                raise RuntimeError(log_error) from exc
            return _record_with_error(record, log_error)

    async def _legacy_commit(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        *,
        sequence: int,
        dry_run: bool,
    ) -> EffectCommitRecord:
        record = await self._committer.commit(
            effect,
            ctx,
            sequence=sequence,
            dry_run=dry_run,
        )
        if record.status == EFFECT_STATUS_DUPLICATE:
            return record
        try:
            audit_record = await self._audit_log.record(
                idempotency_key=record.idempotency_key,
                tenant_id=ctx.event.tenant_id,
                session_id=ctx.event.session_id,
                trace_id=ctx.event.trace_id or ctx.trace_id,
                owner=record.owner,
                type=record.type,
                payload=dict(record.payload),
                status=record.status,
                dry_run=record.dry_run,
            )
            if audit_record.status == EFFECT_STATUS_DUPLICATE:
                return _record_with_producer_owner(
                    _record_with_tenant(audit_record, ctx.event.tenant_id),
                    record.producer_owner,
                )
        except Exception as exc:
            error = _effect_log_error(exc)
            if self._fail_closed:
                raise RuntimeError(error) from exc
            return EffectCommitRecord(
                type=record.type,
                owner=record.owner,
                idempotency_key=record.idempotency_key,
                producer_owner=record.producer_owner,
                payload=dict(record.payload),
                status=record.status,
                error=error,
                dry_run=record.dry_run,
                tenant_id=record.tenant_id,
            )
        return record

    def _resolve_active_claim(
        self,
        record: EffectCommitRecord,
    ) -> tuple[tuple[str, str, bool], EffectCommitRecord]:
        identity = effect_identity(
            record.tenant_id,
            record.idempotency_key,
            dry_run=record.dry_run,
        )
        existing = self._active_claims.get(identity)
        if existing is not None:
            return identity, existing
        if record.tenant_id:
            return identity, record

        matches = [
            (candidate_identity, candidate)
            for candidate_identity, candidate in self._active_claims.items()
            if candidate_identity[1:] == identity[1:]
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError("effect_claim_identity_ambiguous")
        return identity, record


async def mark_effect_completed(
    committer: object,
    record: EffectCommitRecord,
) -> EffectCommitRecord:
    """Finalize a lifecycle-aware committer, otherwise preserve legacy behavior."""

    callback = getattr(committer, "mark_completed", None)
    if record.status != EFFECT_STATUS_RUNNING or not callable(callback):
        return record
    completed = await callback(record)
    return completed if isinstance(completed, EffectCommitRecord) else record


async def mark_effect_failed(
    committer: object,
    record: EffectCommitRecord,
    *,
    error: str,
) -> EffectCommitRecord:
    """Fail a lifecycle-aware claim, otherwise preserve legacy behavior."""

    callback = getattr(committer, "mark_failed", None)
    if record.status != EFFECT_STATUS_RUNNING or not callable(callback):
        return record
    failed = await callback(record, error=error)
    return failed if isinstance(failed, EffectCommitRecord) else record


def normalize_effect(
    effect: MessageEffect,
    ctx: PipelineContext,
    *,
    sequence: int = 0,
) -> MessageEffect:
    effect_type = str(effect.type or "").strip()
    owner = str(effect.owner or "").strip()
    if not effect_type:
        raise ValueError("effect type cannot be empty")
    if not owner:
        raise ValueError("effect owner cannot be empty")
    key = str(effect.idempotency_key or "").strip()
    if not key:
        key = _default_idempotency_key(effect, ctx, sequence=sequence)
    return MessageEffect(
        type=effect_type,
        owner=owner,
        payload=dict(effect.payload),
        idempotency_key=key,
        producer_owner=str(effect.producer_owner or "").strip() or owner,
    )


def effect_identity(
    tenant_id: str,
    idempotency_key: str,
    *,
    dry_run: bool,
) -> tuple[str, str, bool]:
    """Return the canonical tenant-scoped identity used by every effect gate."""

    return (
        str(tenant_id or "").strip(),
        str(idempotency_key or "").strip(),
        bool(dry_run),
    )


def _default_idempotency_key(
    effect: MessageEffect,
    ctx: PipelineContext,
    *,
    sequence: int,
) -> str:
    event = ctx.event
    parts = [
        "effect",
        str(event.tenant_id or ""),
        str(event.session_id or ""),
        str(event.trace_id or ctx.trace_id or ""),
        str(effect.owner or ""),
        str(effect.type or ""),
        str(sequence),
    ]
    return ":".join(_key_part(part) for part in parts)


def _key_part(value: str) -> str:
    cleaned = str(value or "").strip()
    return cleaned.replace(":", "_") or "-"


def _redis_key_part(value: str) -> str:
    return quote(str(value or ""), safe="") or "-"


def _record_envelope(
    record: EffectCommitRecord,
    ctx: PipelineContext,
    *,
    sequence: int,
) -> dict[str, Any]:
    event = ctx.event
    return {
        "type": record.type,
        "owner": record.owner,
        "producer_owner": record.producer_owner,
        "idempotency_key": record.idempotency_key,
        "payload": dict(record.payload),
        "status": record.status,
        "dry_run": record.dry_run,
        "tenant_id": record.tenant_id or event.tenant_id,
        "channel": getattr(event.channel, "value", str(event.channel)),
        "session_id": event.session_id,
        "user_id": event.user_id,
        "message_id": event.message_id,
        "trace_id": event.trace_id or ctx.trace_id,
        "sequence": sequence,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _assert_same_effect_request(
    *,
    existing_type: str,
    existing_owner: str,
    existing_producer_owner: str,
    existing_payload: dict[str, Any],
    requested: MessageEffect,
) -> None:
    normalized_existing_owner = str(existing_owner or "").strip()
    existing = {
        "type": str(existing_type or "").strip(),
        "owner": normalized_existing_owner,
        "producer_owner": (
            str(existing_producer_owner or "").strip() or normalized_existing_owner
        ),
        "payload": dict(existing_payload),
    }
    normalized_requested_owner = str(requested.owner or "").strip()
    current = {
        "type": str(requested.type or "").strip(),
        "owner": normalized_requested_owner,
        "producer_owner": (
            str(requested.producer_owner or "").strip() or normalized_requested_owner
        ),
        "payload": dict(requested.payload),
    }
    if _json_dumps(existing) != _json_dumps(current):
        raise EffectIdempotencyConflictError()


def _effect_log_error(exc: Exception) -> str:
    return f"effect_log_failed:{str(exc).strip() or exc.__class__.__name__}"


def _record_with_error(record: EffectCommitRecord, error: str) -> EffectCommitRecord:
    return EffectCommitRecord(
        type=record.type,
        owner=record.owner,
        idempotency_key=record.idempotency_key,
        producer_owner=record.producer_owner,
        payload=dict(record.payload),
        status=record.status,
        error=error,
        dry_run=record.dry_run,
        tenant_id=record.tenant_id,
        claim_owner=record.claim_owner,
        lease_expires_at=record.lease_expires_at,
        attempt=record.attempt,
    )


def _record_with_tenant(record: EffectCommitRecord, tenant_id: str) -> EffectCommitRecord:
    tenant = str(tenant_id or "").strip()
    if record.tenant_id == tenant:
        return record
    return EffectCommitRecord(
        type=record.type,
        owner=record.owner,
        idempotency_key=record.idempotency_key,
        producer_owner=record.producer_owner,
        payload=dict(record.payload),
        status=record.status,
        error=record.error,
        dry_run=record.dry_run,
        tenant_id=tenant,
        claim_owner=record.claim_owner,
        lease_expires_at=record.lease_expires_at,
        attempt=record.attempt,
    )


def _record_with_producer_owner(
    record: EffectCommitRecord,
    producer_owner: str,
) -> EffectCommitRecord:
    producer = str(producer_owner or "").strip() or str(record.owner or "").strip()
    if record.producer_owner == producer:
        return record
    return EffectCommitRecord(
        type=record.type,
        owner=record.owner,
        idempotency_key=record.idempotency_key,
        producer_owner=producer,
        payload=dict(record.payload),
        status=record.status,
        error=record.error,
        dry_run=record.dry_run,
        tenant_id=record.tenant_id,
        claim_owner=record.claim_owner,
        lease_expires_at=record.lease_expires_at,
        attempt=record.attempt,
    )
