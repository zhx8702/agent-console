"""Durable, shared speaking budget for every automated group output path."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from prometheus_client import Counter
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.infra.db import get_engine
from app.social.contracts import ParticipationPolicyValues
from app.social.reply_style import ReplyStyleHistory, text_fingerprint

_BOT_OUTPUT_KINDS = frozenset({"ordinary", "proactive", "repeater", "report"})
_SPEECH_CLASSES = frozenset(
    {"obligation", "required_delivery", "soft", "scheduled"}
)
_ACTIVE_STATUSES = frozenset({"reserved", "committed"})
_RESERVATION_TTL = timedelta(minutes=5)

SOCIAL_SPEECH_RESERVATIONS = Counter(
    "agent_console_social_speech_reservations_total",
    "Group speech reservation outcomes across all automated output paths.",
    ("output_kind", "outcome", "reason"),
)
SOCIAL_SPEECH_TRANSITIONS = Counter(
    "agent_console_social_speech_transitions_total",
    "Group speech ledger state transitions.",
    ("transition", "output_kind"),
)
SOCIAL_SPEECH_OBSERVATIONS = Counter(
    "agent_console_social_speech_observations_total",
    "Durable group-message observations merged into the shared speech ledger.",
    ("author_kind",),
)


def _group_advisory_scope(tenant_id: str, session_id: str) -> str:
    """Build a collision-free PostgreSQL text key without forbidden NUL bytes."""

    return f"social-speech-v1:{len(tenant_id)}:{tenant_id}{session_id}"


@dataclass(frozen=True, slots=True)
class SpeechBudgetPolicy:
    max_bot_messages_10m: int = 2
    max_bot_messages_hour: int = 6
    max_bot_ratio_last_40: float = 0.15
    max_consecutive_bot_messages: int = 2


@dataclass(frozen=True, slots=True)
class SpeechBudgetSnapshot:
    recent_author_kinds: tuple[str, ...] = ()  # newest first, at most 39
    bot_messages_10m: int = 0
    bot_messages_hour: int = 0


@dataclass(frozen=True, slots=True)
class SpeechBudgetDecision:
    allowed: bool
    reason: str
    prospective_bot_ratio: float
    consecutive_bot_messages: int


@dataclass(frozen=True, slots=True)
class SpeechReservation:
    allowed: bool
    idempotency_key: str
    output_kind: str
    speech_class: str = "soft"
    reason: str = "allowed"
    reservation_id: str = ""
    replayed: bool = False
    snapshot: SpeechBudgetSnapshot | None = None
    policy_version: int = 0


class GroupSpeechBudgetExceeded(RuntimeError):
    def __init__(self, reason: str, *, output_kind: str, idempotency_key: str) -> None:
        super().__init__(f"group speech budget denied: {reason}")
        self.reason = reason
        self.output_kind = output_kind
        self.idempotency_key = idempotency_key


class GroupSpeechLedgerProtocol(Protocol):
    async def reserve(
        self,
        *,
        tenant_id: str,
        session_id: str,
        idempotency_key: str,
        output_kind: str,
        speech_class: str = "soft",
        text: str = "",
        emoji: str = "",
        catchphrase: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SpeechReservation: ...

    async def commit(
        self,
        reservation: SpeechReservation,
        *,
        provider_message_id: str = "",
    ) -> None: ...

    async def release(self, reservation: SpeechReservation, *, reason: str) -> None: ...

    async def observe_message(
        self,
        *,
        tenant_id: str,
        session_id: str,
        message_id: str,
        is_bot: bool,
        text: str = "",
        occurred_at: datetime | None = None,
    ) -> None: ...

    async def recent_style_history(
        self,
        tenant_id: str,
        session_id: str,
    ) -> ReplyStyleHistory: ...

    async def has_near_duplicate(
        self,
        *,
        tenant_id: str,
        session_id: str,
        text: str,
        idempotency_key: str = "",
    ) -> bool: ...


def evaluate_speech_budget(
    snapshot: SpeechBudgetSnapshot,
    policy: SpeechBudgetPolicy | None = None,
    *,
    speech_class: str = "soft",
) -> SpeechBudgetDecision:
    active = policy or SpeechBudgetPolicy()
    recent = tuple(snapshot.recent_author_kinds[:39])
    consecutive = 0
    for author_kind in recent:
        if author_kind != "bot":
            break
        consecutive += 1
    prospective = ("bot", *recent)
    bot_count = sum(1 for author_kind in prospective if author_kind == "bot")
    ratio = bot_count / len(prospective) if prospective else 0.0

    normalized_class = _normalize_speech_class(speech_class)

    # Explicitly subscribed or operator-triggered deliveries have their own
    # cadence, scope and idempotency controls. Keep them in the durable ledger
    # for audit, but do not let conversational volume or sequence budgets
    # suppress them.
    if normalized_class == "required_delivery":
        return SpeechBudgetDecision(
            True,
            "required_delivery_bypass",
            ratio,
            consecutive,
        )

    # A direct address, command, safety/privacy control or quoted reply is a
    # conversation obligation.  It bypasses volume and ratio budgets, but it
    # may not create a third consecutive bot message.  The durable queue keeps
    # that obligation pending until a human turn breaks the run.
    if normalized_class == "obligation":
        if (
            active.max_consecutive_bot_messages > 0
            and consecutive >= active.max_consecutive_bot_messages
        ):
            return SpeechBudgetDecision(
                False,
                "third_consecutive_bot_message",
                ratio,
                consecutive,
            )
        return SpeechBudgetDecision(True, "obligation_bypass", ratio, consecutive)

    if snapshot.bot_messages_10m >= active.max_bot_messages_10m:
        return SpeechBudgetDecision(False, "budget_10m", ratio, consecutive)
    if snapshot.bot_messages_hour >= active.max_bot_messages_hour:
        return SpeechBudgetDecision(False, "budget_hour", ratio, consecutive)
    if consecutive >= active.max_consecutive_bot_messages:
        return SpeechBudgetDecision(False, "third_consecutive_bot_message", ratio, consecutive)
    if ratio > active.max_bot_ratio_last_40:
        return SpeechBudgetDecision(False, "bot_ratio_last_40", ratio, consecutive)
    return SpeechBudgetDecision(True, "allowed", ratio, consecutive)


async def reserve_or_raise(
    ledger: GroupSpeechLedgerProtocol,
    **kwargs: Any,
) -> SpeechReservation:
    reservation = await ledger.reserve(**kwargs)
    if not reservation.allowed:
        raise GroupSpeechBudgetExceeded(
            reservation.reason,
            output_kind=reservation.output_kind,
            idempotency_key=reservation.idempotency_key,
        )
    return reservation


class GroupSpeechLedger:
    """PostgreSQL implementation serialized by a per-group advisory lock."""

    def __init__(
        self,
        engine: AsyncEngine | None = None,
    ) -> None:
        self._engine = engine or get_engine()

    async def reserve(
        self,
        *,
        tenant_id: str,
        session_id: str,
        idempotency_key: str,
        output_kind: str,
        speech_class: str = "soft",
        text: str = "",
        emoji: str = "",
        catchphrase: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SpeechReservation:
        tenant, session, key, kind = _normalize_request(
            tenant_id,
            session_id,
            idempotency_key,
            output_kind,
        )
        speech = _normalize_speech_class(speech_class)
        fingerprint = text_fingerprint(text)
        async with self._engine.begin() as conn:
            await conn.execute(
                sql_text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
                {"scope": _group_advisory_scope(tenant, session)},
            )
            await conn.execute(
                sql_text(
                    "UPDATE social_group_speech_ledger SET status = 'released', "
                    "released_at = NOW(), release_reason = 'reservation_expired' "
                    "WHERE tenant_id = :tenant AND session_id = :session "
                    "AND status = 'reserved' AND reserved_at < NOW() - INTERVAL '5 minutes'"
                ),
                {"tenant": tenant, "session": session},
            )
            existing = (
                await conn.execute(
                    sql_text(
                        "SELECT id, status, output_kind, text_fingerprint, metadata_json "
                        "FROM social_group_speech_ledger "
                        "WHERE tenant_id = :tenant AND session_id = :session "
                        "AND idempotency_key = :key LIMIT 1"
                    ),
                    {"tenant": tenant, "session": session, "key": key},
                )
            ).mappings().first()
            if existing and str(existing["status"]) in _ACTIVE_STATUSES:
                existing_metadata = existing.get("metadata_json")
                if isinstance(existing_metadata, str):
                    try:
                        existing_metadata = json.loads(existing_metadata)
                    except json.JSONDecodeError:
                        existing_metadata = {}
                existing_class = (
                    str(existing_metadata.get("speech_class") or "soft")
                    if isinstance(existing_metadata, dict)
                    else "soft"
                )
                if (
                    str(existing.get("output_kind") or "") != kind
                    or existing_class != speech
                    or str(existing.get("text_fingerprint") or "") != fingerprint
                ):
                    raise ValueError(
                        "speech idempotency key reused with different output"
                    )
                result = SpeechReservation(
                    allowed=True,
                    idempotency_key=key,
                    output_kind=kind,
                    speech_class=speech,
                    reservation_id=str(existing["id"]),
                    replayed=True,
                    policy_version=_metadata_policy_version(existing_metadata),
                )
                _record_reservation_metric(result)
                return result

            policy_version, active_policy = await self._current_policy(
                conn,
                tenant,
                session,
            )
            snapshot = await self._snapshot(conn, tenant, session)
            decision = evaluate_speech_budget(
                snapshot,
                active_policy,
                speech_class=speech,
            )
            if not decision.allowed:
                result = SpeechReservation(
                    allowed=False,
                    idempotency_key=key,
                    output_kind=kind,
                    speech_class=speech,
                    reason=decision.reason,
                    snapshot=snapshot,
                    policy_version=policy_version,
                )
                _record_reservation_metric(result)
                return result

            reservation_id = str(existing["id"]) if existing else str(uuid4())
            safe_metadata = dict(metadata or {})
            safe_metadata["speech_class"] = speech
            safe_metadata["participation_policy_version"] = policy_version
            safe_metadata["near_duplicate_signature"] = near_duplicate_signature(text)
            params = {
                "id": reservation_id,
                "tenant": tenant,
                "session": session,
                "key": key,
                "kind": kind,
                "fingerprint": fingerprint,
                "emoji": str(emoji or "")[:32],
                "catchphrase": str(catchphrase or "")[:64],
                "metadata": json.dumps(safe_metadata, ensure_ascii=False, default=str),
            }
            if existing:
                await conn.execute(
                    sql_text(
                        "UPDATE social_group_speech_ledger SET output_kind = :kind, "
                        "author_kind = 'bot', status = 'reserved', "
                        "text_fingerprint = :fingerprint, emoji = :emoji, "
                        "catchphrase = :catchphrase, provider_message_id = '', "
                        "reserved_at = NOW(), committed_at = NULL, occurred_at = NOW(), "
                        "released_at = NULL, release_reason = '', "
                        "metadata_json = CAST(:metadata AS JSONB) WHERE id = :id"
                    ),
                    params,
                )
            else:
                await conn.execute(
                    sql_text(
                        "INSERT INTO social_group_speech_ledger "
                        "(id, tenant_id, session_id, idempotency_key, output_kind, "
                        " author_kind, status, text_fingerprint, emoji, catchphrase, "
                        " metadata_json, reserved_at, occurred_at) VALUES "
                        "(:id, :tenant, :session, :key, :kind, 'bot', 'reserved', "
                        " :fingerprint, :emoji, :catchphrase, CAST(:metadata AS JSONB), "
                        " NOW(), NOW())"
                    ),
                    params,
                )

        result = SpeechReservation(
            allowed=True,
            idempotency_key=key,
            output_kind=kind,
            speech_class=speech,
            reservation_id=reservation_id,
            snapshot=snapshot,
            policy_version=policy_version,
        )
        _record_reservation_metric(result)
        return result

    async def _current_policy(
        self,
        conn: Any,
        tenant_id: str,
        session_id: str,
    ) -> tuple[int, SpeechBudgetPolicy]:
        """Load and pin the current versioned group policy inside the lock.

        The advisory lock serializes speech reservations for this group.  The
        row share lock additionally prevents a concurrent policy replacement
        from changing the version while this reservation is evaluated.
        """

        row = (
            await conn.execute(
                sql_text(
                    "SELECT version, policy_json FROM social_group_policy "
                    "WHERE tenant_id = :tenant AND session_id = :session "
                    "LIMIT 1 FOR SHARE"
                ),
                {"tenant": tenant_id, "session": session_id},
            )
        ).mappings().first()
        if not row:
            return 0, _speech_budget_policy_from_values({})
        raw_policy = row.get("policy_json")
        if isinstance(raw_policy, str):
            try:
                raw_policy = json.loads(raw_policy)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid versioned group speech policy") from exc
        if not isinstance(raw_policy, dict):
            raise ValueError("invalid versioned group speech policy")
        return (
            max(0, int(row.get("version") or 0)),
            _speech_budget_policy_from_values(raw_policy),
        )

    async def _snapshot(self, conn: Any, tenant_id: str, session_id: str) -> SpeechBudgetSnapshot:
        counts = (
            await conn.execute(
                sql_text(
                    "SELECT "
                    "COUNT(*) FILTER (WHERE author_kind = 'bot' "
                    " AND occurred_at >= NOW() - INTERVAL '10 minutes') AS bot_10m, "
                    "COUNT(*) FILTER (WHERE author_kind = 'bot' "
                    " AND occurred_at >= NOW() - INTERVAL '1 hour') AS bot_hour "
                    "FROM social_group_speech_ledger WHERE tenant_id = :tenant "
                    "AND session_id = :session AND status IN ('reserved', 'committed')"
                ),
                {"tenant": tenant_id, "session": session_id},
            )
        ).mappings().first() or {}
        recent = (
            await conn.execute(
                sql_text(
                    "SELECT author_kind FROM social_group_speech_ledger "
                    "WHERE tenant_id = :tenant AND session_id = :session "
                    "AND status IN ('reserved', 'committed') "
                    "ORDER BY occurred_at DESC, id DESC LIMIT 39"
                ),
                {"tenant": tenant_id, "session": session_id},
            )
        ).mappings().all()
        return SpeechBudgetSnapshot(
            recent_author_kinds=tuple(str(row["author_kind"]) for row in recent),
            bot_messages_10m=int(counts.get("bot_10m") or 0),
            bot_messages_hour=int(counts.get("bot_hour") or 0),
        )

    async def commit(
        self,
        reservation: SpeechReservation,
        *,
        provider_message_id: str = "",
    ) -> None:
        if not reservation.allowed:
            return
        async with self._engine.begin() as conn:
            await conn.execute(
                sql_text(
                    "UPDATE social_group_speech_ledger SET status = 'committed', "
                    "committed_at = COALESCE(committed_at, NOW()), "
                    "provider_message_id = CASE WHEN :provider <> '' THEN :provider "
                    "ELSE provider_message_id END WHERE id = :id "
                    "AND status IN ('reserved', 'committed')"
                ),
                {
                    "id": reservation.reservation_id,
                    "provider": str(provider_message_id or "")[:128],
                },
            )
        SOCIAL_SPEECH_TRANSITIONS.labels(
            transition="commit",
            output_kind=reservation.output_kind,
        ).inc()

    async def release(self, reservation: SpeechReservation, *, reason: str) -> None:
        if not reservation.allowed or not reservation.reservation_id:
            return
        async with self._engine.begin() as conn:
            await conn.execute(
                sql_text(
                    "UPDATE social_group_speech_ledger SET status = 'released', "
                    "released_at = NOW(), release_reason = :reason WHERE id = :id "
                    "AND status = 'reserved'"
                ),
                {
                    "id": reservation.reservation_id,
                    "reason": str(reason or "released")[:64],
                },
            )
        SOCIAL_SPEECH_TRANSITIONS.labels(
            transition="release",
            output_kind=reservation.output_kind,
        ).inc()

    async def observe_message(
        self,
        *,
        tenant_id: str,
        session_id: str,
        message_id: str,
        is_bot: bool,
        text: str = "",
        occurred_at: datetime | None = None,
    ) -> None:
        tenant = str(tenant_id or "").strip()
        session = str(session_id or "").strip()
        message = str(message_id or "").strip()[:128]
        if not tenant or not session or not message:
            return
        observed_at = _aware_utc(occurred_at or datetime.now(UTC))
        fingerprint = text_fingerprint(text)
        async with self._engine.begin() as conn:
            await conn.execute(
                sql_text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
                {"scope": _group_advisory_scope(tenant, session)},
            )
            duplicate = (
                await conn.execute(
                    sql_text(
                        "SELECT id FROM social_group_speech_ledger "
                        "WHERE tenant_id = :tenant AND session_id = :session "
                        "AND observed_message_id = :message LIMIT 1"
                    ),
                    {"tenant": tenant, "session": session, "message": message},
                )
            ).first()
            if duplicate:
                return
            if is_bot:
                matched = (
                    await conn.execute(
                        sql_text(
                            "SELECT id FROM social_group_speech_ledger "
                            "WHERE tenant_id = :tenant AND session_id = :session "
                            "AND author_kind = 'bot' AND status = 'committed' "
                            "AND observed_message_id = '' AND text_fingerprint = :fingerprint "
                            "AND occurred_at >= NOW() - INTERVAL '10 minutes' "
                            "ORDER BY occurred_at DESC LIMIT 1 FOR UPDATE"
                        ),
                        {
                            "tenant": tenant,
                            "session": session,
                            "fingerprint": fingerprint,
                        },
                    )
                ).mappings().first()
                if matched:
                    await conn.execute(
                        sql_text(
                            "UPDATE social_group_speech_ledger SET observed_message_id = :message, "
                            "observed_at = :observed, occurred_at = :observed WHERE id = :id"
                        ),
                        {
                            "message": message,
                            "observed": observed_at,
                            "id": matched["id"],
                        },
                    )
                    SOCIAL_SPEECH_OBSERVATIONS.labels(author_kind="bot").inc()
                    return
            await conn.execute(
                sql_text(
                    "INSERT INTO social_group_speech_ledger "
                    "(id, tenant_id, session_id, idempotency_key, output_kind, author_kind, "
                    " status, text_fingerprint, observed_message_id, observed_at, "
                    " committed_at, occurred_at) VALUES "
                    "(:id, :tenant, :session, :key, :kind, :author, 'committed', "
                    " :fingerprint, :message, :observed, :observed, :observed) "
                    "ON CONFLICT (tenant_id, session_id, observed_message_id) "
                    "WHERE observed_message_id <> '' DO NOTHING"
                ),
                {
                    "id": str(uuid4()),
                    "tenant": tenant,
                    "session": session,
                    "key": f"observation:{message}"[:256],
                    "kind": "external_bot" if is_bot else "human_observation",
                    "author": "bot" if is_bot else "human",
                    "fingerprint": fingerprint,
                    "message": message,
                    "observed": observed_at,
                },
            )
        SOCIAL_SPEECH_OBSERVATIONS.labels(
            author_kind="bot" if is_bot else "human"
        ).inc()

    async def recent_style_history(
        self,
        tenant_id: str,
        session_id: str,
    ) -> ReplyStyleHistory:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sql_text(
                        "SELECT emoji, catchphrase FROM social_group_speech_ledger "
                        "WHERE tenant_id = :tenant AND session_id = :session "
                        "AND author_kind = 'bot' AND status = 'committed' "
                        "ORDER BY occurred_at DESC, id DESC LIMIT 30"
                    ),
                    {"tenant": tenant_id, "session": session_id},
                )
            ).mappings().all()
        return ReplyStyleHistory(
            emojis_last_20=tuple(str(row.get("emoji") or "") for row in rows[:20] if row.get("emoji")),
            catchphrases_last_30=tuple(
                str(row.get("catchphrase") or "") for row in rows if row.get("catchphrase")
            ),
        )

    async def has_near_duplicate(
        self,
        *,
        tenant_id: str,
        session_id: str,
        text: str,
        idempotency_key: str = "",
    ) -> bool:
        fingerprint = text_fingerprint(text)
        signature = near_duplicate_signature(text)
        if not str(text or "").strip():
            return False
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sql_text(
                        "SELECT idempotency_key, text_fingerprint, metadata_json "
                        "FROM social_group_speech_ledger "
                        "WHERE tenant_id = :tenant AND session_id = :session "
                        "AND author_kind = 'bot' AND status IN ('reserved', 'committed') "
                        "AND occurred_at >= NOW() - INTERVAL '24 hours' "
                        "AND idempotency_key <> :key "
                        "ORDER BY occurred_at DESC, id DESC LIMIT 200"
                    ),
                    {
                        "tenant": str(tenant_id or "").strip(),
                        "session": str(session_id or "").strip(),
                        "key": str(idempotency_key or "").strip()[:256],
                    },
                )
            ).mappings().all()
        for row in rows:
            if str(row.get("text_fingerprint") or "") == fingerprint:
                return True
            metadata = row.get("metadata_json")
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = {}
            previous = (
                str(metadata.get("near_duplicate_signature") or "")
                if isinstance(metadata, dict)
                else ""
            )
            if _signatures_near(signature, previous):
                return True
        return False


@dataclass(slots=True)
class _MemorySpeechEvent:
    reservation_id: str
    tenant_id: str
    session_id: str
    idempotency_key: str
    output_kind: str
    speech_class: str
    author_kind: str
    status: str
    occurred_at: datetime
    policy_version: int = 0
    text_fingerprint: str = ""
    emoji: str = ""
    catchphrase: str = ""
    near_duplicate_signature: str = ""
    observed_message_id: str = ""


class InMemoryGroupSpeechLedger:
    """Deterministic test implementation with production-equivalent policy."""

    def __init__(
        self,
        *,
        policy: SpeechBudgetPolicy | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = policy or SpeechBudgetPolicy()
        self._now = now or (lambda: datetime.now(UTC))
        self.events: list[_MemorySpeechEvent] = []
        self._group_policies: dict[tuple[str, str], tuple[int, SpeechBudgetPolicy]] = {}

    def set_group_policy(
        self,
        tenant_id: str,
        session_id: str,
        *,
        version: int,
        policy: SpeechBudgetPolicy,
    ) -> None:
        """Install the current versioned policy for a deterministic test group."""

        self._group_policies[(tenant_id, session_id)] = (
            max(0, int(version)),
            policy,
        )

    async def reserve(self, **kwargs: Any) -> SpeechReservation:
        tenant, session, key, kind = _normalize_request(
            kwargs.get("tenant_id"),
            kwargs.get("session_id"),
            kwargs.get("idempotency_key"),
            kwargs.get("output_kind"),
        )
        speech = _normalize_speech_class(kwargs.get("speech_class"))
        now = _aware_utc(self._now())
        fingerprint = text_fingerprint(kwargs.get("text") or "")
        for event in self.events:
            if (
                event.tenant_id == tenant
                and event.session_id == session
                and event.idempotency_key == key
                and event.status in _ACTIVE_STATUSES
            ):
                if (
                    event.output_kind != kind
                    or event.speech_class != speech
                    or event.text_fingerprint != fingerprint
                ):
                    raise ValueError(
                        "speech idempotency key reused with different output"
                    )
                result = SpeechReservation(
                    True,
                    key,
                    kind,
                    speech,
                    reservation_id=event.reservation_id,
                    replayed=True,
                    policy_version=event.policy_version,
                )
                _record_reservation_metric(result)
                return result
        for event in self.events:
            if (
                event.tenant_id == tenant
                and event.session_id == session
                and event.status == "reserved"
                and now - event.occurred_at > _RESERVATION_TTL
            ):
                event.status = "released"
        active = sorted(
            (
                event
                for event in self.events
                if event.tenant_id == tenant
                and event.session_id == session
                and event.status in _ACTIVE_STATUSES
            ),
            key=lambda event: event.occurred_at,
            reverse=True,
        )
        snapshot = SpeechBudgetSnapshot(
            recent_author_kinds=tuple(event.author_kind for event in active[:39]),
            bot_messages_10m=sum(
                1
                for event in active
                if event.author_kind == "bot" and now - event.occurred_at <= timedelta(minutes=10)
            ),
            bot_messages_hour=sum(
                1
                for event in active
                if event.author_kind == "bot" and now - event.occurred_at <= timedelta(hours=1)
            ),
        )
        policy_version, active_policy = self._group_policies.get(
            (tenant, session),
            (0, self._policy),
        )
        decision = evaluate_speech_budget(
            snapshot,
            active_policy,
            speech_class=speech,
        )
        if not decision.allowed:
            result = SpeechReservation(
                False,
                key,
                kind,
                speech,
                reason=decision.reason,
                snapshot=snapshot,
                policy_version=policy_version,
            )
            _record_reservation_metric(result)
            return result
        existing = next(
            (
                event
                for event in self.events
                if event.tenant_id == tenant
                and event.session_id == session
                and event.idempotency_key == key
            ),
            None,
        )
        if existing is None:
            existing = _MemorySpeechEvent(
                reservation_id=str(uuid4()),
                tenant_id=tenant,
                session_id=session,
                idempotency_key=key,
                output_kind=kind,
                speech_class=speech,
                author_kind="bot",
                status="reserved",
                occurred_at=now,
            )
            self.events.append(existing)
        existing.output_kind = kind
        existing.speech_class = speech
        existing.author_kind = "bot"
        existing.status = "reserved"
        existing.occurred_at = now
        existing.policy_version = policy_version
        existing.text_fingerprint = fingerprint
        existing.emoji = str(kwargs.get("emoji") or "")
        existing.catchphrase = str(kwargs.get("catchphrase") or "")
        existing.near_duplicate_signature = near_duplicate_signature(
            kwargs.get("text") or ""
        )
        result = SpeechReservation(
            True,
            key,
            kind,
            speech,
            reservation_id=existing.reservation_id,
            snapshot=snapshot,
            policy_version=policy_version,
        )
        _record_reservation_metric(result)
        return result

    async def commit(self, reservation: SpeechReservation, **_: Any) -> None:
        event = self._find(reservation.reservation_id)
        if event and event.status in _ACTIVE_STATUSES:
            event.status = "committed"

    async def release(self, reservation: SpeechReservation, *, reason: str) -> None:
        _ = reason
        event = self._find(reservation.reservation_id)
        if event and event.status == "reserved":
            event.status = "released"

    async def observe_message(self, **kwargs: Any) -> None:
        tenant = str(kwargs.get("tenant_id") or "").strip()
        session = str(kwargs.get("session_id") or "").strip()
        message = str(kwargs.get("message_id") or "").strip()[:128]
        if not tenant or not session or not message:
            return
        if any(
            event.tenant_id == tenant
            and event.session_id == session
            and event.observed_message_id == message
            for event in self.events
        ):
            return
        now = _aware_utc(kwargs.get("occurred_at") or self._now())
        is_bot = bool(kwargs.get("is_bot"))
        fingerprint = text_fingerprint(kwargs.get("text") or "")
        if is_bot:
            for event in sorted(self.events, key=lambda item: item.occurred_at, reverse=True):
                if (
                    event.tenant_id == tenant
                    and event.session_id == session
                    and event.author_kind == "bot"
                    and event.status == "committed"
                    and not event.observed_message_id
                    and event.text_fingerprint == fingerprint
                    and now - event.occurred_at <= timedelta(minutes=10)
                ):
                    event.observed_message_id = message
                    event.occurred_at = now
                    return
        self.events.append(
            _MemorySpeechEvent(
                reservation_id=str(uuid4()),
                tenant_id=tenant,
                session_id=session,
                idempotency_key=f"observation:{message}",
                output_kind="external_bot" if is_bot else "human_observation",
                speech_class="soft" if is_bot else "observation",
                author_kind="bot" if is_bot else "human",
                status="committed",
                occurred_at=now,
                text_fingerprint=fingerprint,
                near_duplicate_signature=near_duplicate_signature(
                    kwargs.get("text") or ""
                ),
                observed_message_id=message,
            )
        )

    async def recent_style_history(self, tenant_id: str, session_id: str) -> ReplyStyleHistory:
        bot_events = sorted(
            (
                event
                for event in self.events
                if event.tenant_id == tenant_id
                and event.session_id == session_id
                and event.author_kind == "bot"
                and event.status == "committed"
            ),
            key=lambda event: event.occurred_at,
            reverse=True,
        )[:30]
        return ReplyStyleHistory(
            emojis_last_20=tuple(event.emoji for event in bot_events[:20] if event.emoji),
            catchphrases_last_30=tuple(
                event.catchphrase for event in bot_events if event.catchphrase
            ),
        )

    async def has_near_duplicate(
        self,
        *,
        tenant_id: str,
        session_id: str,
        text: str,
        idempotency_key: str = "",
    ) -> bool:
        if not str(text or "").strip():
            return False
        now = _aware_utc(self._now())
        fingerprint = text_fingerprint(text)
        signature = near_duplicate_signature(text)
        for event in self.events:
            if (
                event.tenant_id != tenant_id
                or event.session_id != session_id
                or event.idempotency_key == idempotency_key
                or event.author_kind != "bot"
                or event.status not in _ACTIVE_STATUSES
                or now - event.occurred_at > timedelta(hours=24)
            ):
                continue
            if event.text_fingerprint == fingerprint or _signatures_near(
                signature,
                event.near_duplicate_signature,
            ):
                return True
        return False

    def _find(self, reservation_id: str) -> _MemorySpeechEvent | None:
        return next(
            (event for event in self.events if event.reservation_id == reservation_id),
            None,
        )


def derive_speech_idempotency_key(
    *,
    tenant_id: str,
    session_id: str,
    command_id: str,
    trace_id: str,
    source_message_id: str,
    output_kind: str,
    text: str,
) -> str:
    explicit = str(command_id or "").strip()
    if explicit:
        return explicit[:256]
    payload = "\0".join(
        (
            str(tenant_id or ""),
            str(session_id or ""),
            str(trace_id or ""),
            str(source_message_id or ""),
            str(output_kind or ""),
            text_fingerprint(text),
        )
    )
    return f"speech:{text_fingerprint(payload)}"


def _normalize_request(
    tenant_id: Any,
    session_id: Any,
    idempotency_key: Any,
    output_kind: Any,
) -> tuple[str, str, str, str]:
    tenant = str(tenant_id or "").strip()
    session = str(session_id or "").strip()
    key = str(idempotency_key or "").strip()[:256]
    kind = str(output_kind or "ordinary").strip().lower()
    if not tenant or not session or not key:
        raise ValueError("tenant_id, session_id and idempotency_key are required")
    if kind not in _BOT_OUTPUT_KINDS:
        raise ValueError(f"unsupported speech output kind: {kind}")
    return tenant, session, key, kind


def _normalize_speech_class(value: Any) -> str:
    speech_class = str(value or "soft").strip().lower()
    if speech_class not in _SPEECH_CLASSES:
        raise ValueError(f"unsupported speech class: {speech_class}")
    return speech_class


def _speech_budget_policy_from_values(value: dict[str, Any]) -> SpeechBudgetPolicy:
    """Project the canonical versioned participation policy onto ledger limits."""

    policy = ParticipationPolicyValues.model_validate(value)
    return SpeechBudgetPolicy(
        max_bot_messages_10m=policy.max_soft_replies_10m,
        max_bot_messages_hour=policy.max_soft_replies_hour,
        max_bot_ratio_last_40=policy.max_bot_ratio_last_40,
        max_consecutive_bot_messages=policy.max_consecutive_bot_messages,
    )


def _metadata_policy_version(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    try:
        return max(0, int(value.get("participation_policy_version") or 0))
    except (TypeError, ValueError):
        return 0


def near_duplicate_signature(value: Any) -> str:
    """Return a content-free 64-bit SimHash for a reply.

    The ledger deliberately stores only a one-way fingerprint/signature, never
    reply text.  Character shingles make punctuation and small filler changes
    compare as near duplicates while preventing message reconstruction.
    """

    normalized = unicodedata.normalize("NFKC", str(value or "")).lower()
    normalized = re.sub(r"\s+", "", normalized)
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", normalized)
    if not normalized:
        return ""
    shingles = (
        [normalized]
        if len(normalized) < 3
        else [normalized[index : index + 3] for index in range(len(normalized) - 2)]
    )
    weights = [0] * 64
    for shingle in shingles:
        digest = int(text_fingerprint(shingle)[:16], 16)
        for bit in range(64):
            weights[bit] += 1 if digest & (1 << bit) else -1
    signature = sum(1 << bit for bit, weight in enumerate(weights) if weight >= 0)
    return f"{signature:016x}"


def _signatures_near(left: str, right: str, *, max_distance: int = 3) -> bool:
    if len(left) != 16 or len(right) != 16:
        return False
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count() <= max_distance
    except ValueError:
        return False


def _record_reservation_metric(reservation: SpeechReservation) -> None:
    outcome = "replay" if reservation.replayed else "allowed" if reservation.allowed else "denied"
    SOCIAL_SPEECH_RESERVATIONS.labels(
        output_kind=reservation.output_kind,
        outcome=outcome,
        reason=reservation.reason,
    ).inc()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
