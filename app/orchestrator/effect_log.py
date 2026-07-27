"""Durable execution state machine for message-flow effects.

The table is migration-owned.  Runtime startup validates the Alembic revision
and required structure but never creates or alters production schema.  Tests
and compatibility tools may opt in to a create-only bootstrap explicitly.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypedDict

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.orchestrator.effects import (
    EFFECT_LIFECYCLE_STATUSES,
    EFFECT_STATUS_COMPLETED,
    EFFECT_STATUS_DRY_RUN,
    EFFECT_STATUS_DUPLICATE,
    EFFECT_STATUS_FAILED,
    EFFECT_STATUS_PREPARED,
    EFFECT_STATUS_RECORDED,
    EFFECT_STATUS_RUNNING,
    EffectClaimLost,
    EffectClaimUnavailable,
    EffectCommitRecord,
)

EFFECT_LOG_SCHEMA_REVISION = "0012_flow_effect_state_machine"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REQUIRED_COLUMNS = frozenset(
    {
        "id",
        "idempotency_key",
        "tenant_id",
        "session_id",
        "trace_id",
        "owner",
        "type",
        "status",
        "dry_run",
        "payload",
        "claim_owner",
        "lease_expires_at",
        "attempt",
        "last_error",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "failed_at",
    }
)


class EffectLogSchemaError(RuntimeError):
    """Raised when the migration-owned effect table is missing or stale."""


class _TableStructure(TypedDict):
    exists: bool
    columns: list[str]
    unique_columns: list[tuple[str, ...]]
    checks: list[str]


class PostgresEffectLog:
    """SQL effect lifecycle store with PostgreSQL and SQLite test support."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        session_scope: Callable[[], Any] | None = None,
        table_name: str = "flow_effect_log",
        required_revision: str = EFFECT_LOG_SCHEMA_REVISION,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if session_factory is not None and session_scope is not None:
            raise ValueError("pass either session_factory or session_scope, not both")
        self._session_factory = session_factory
        self._session_scope = session_scope
        self._table_name = _validate_identifier(table_name)
        self._required_revision = _required(required_revision, "required_revision")
        self._clock = clock or (lambda: datetime.now(UTC))

    async def create_schema(self) -> None:
        """Reject the removed runtime-DDL API; Alembic is the sole schema owner."""

        raise EffectLogSchemaError(
            "runtime effect-log DDL is disabled; run `alembic upgrade head`"
        )

    async def ensure_schema(self) -> None:
        """Validate migration revision and table structure without production DDL."""

        await self._validate_schema(check_revision=True)

    async def prepare(
        self,
        *,
        idempotency_key: str,
        tenant_id: str,
        session_id: str,
        trace_id: str,
        owner: str,
        type: str,
        payload: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> EffectCommitRecord:
        """Durably insert ``prepared`` without treating other states as duplicate."""

        key = _required(idempotency_key, "idempotency_key")
        tenant = str(tenant_id or "").strip()
        effect_owner = _required(owner, "owner")
        effect_type = _required(type, "type")
        payload_value = dict(payload or {})
        now = _as_utc(self._clock())

        async with self._db() as db:
            dialect = _dialect_name(db)
            await db.execute(
                text(self._insert_prepared_sql(dialect)),
                {
                    "idempotency_key": key,
                    "tenant_id": tenant,
                    "session_id": str(session_id or ""),
                    "trace_id": str(trace_id or ""),
                    "owner": effect_owner,
                    "type": effect_type,
                    "status": EFFECT_STATUS_PREPARED,
                    "dry_run": bool(dry_run),
                    "payload": _json_dumps(payload_value),
                    "now": _timestamp_param(dialect, now),
                },
            )
            existing = await self._fetch_existing(
                db,
                tenant,
                key,
                dry_run=dry_run,
            )
        if existing is None:
            raise RuntimeError("effect_prepare_missing_after_insert")
        return _row_to_record(existing)

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
        now: datetime | None = None,
    ) -> EffectCommitRecord:
        """Atomically claim an executable effect or report a completed duplicate."""

        key = _required(idempotency_key, "idempotency_key")
        tenant = str(tenant_id or "").strip()
        worker = _required(claim_owner, "claim_owner")
        await self.prepare(
            idempotency_key=key,
            tenant_id=tenant,
            session_id=session_id,
            trace_id=trace_id,
            owner=owner,
            type=type,
            payload=payload,
            dry_run=dry_run,
        )

        claimed_at = _as_utc(now or self._clock())
        lease_expires_at = claimed_at + timedelta(seconds=max(1, int(lease_seconds or 1)))
        async with self._db() as db:
            dialect = _dialect_name(db)
            timestamp = _timestamp_param(dialect, claimed_at)
            lease = _timestamp_param(dialect, lease_expires_at)
            if dry_run:
                result = await db.execute(
                    text(
                        f"""
                        UPDATE {self._table_name}
                        SET status = :completed,
                            claim_owner = :claim_owner,
                            attempt = attempt + 1,
                            lease_expires_at = NULL,
                            completed_at = :now,
                            updated_at = :now,
                            last_error = ''
                        WHERE idempotency_key = :idempotency_key
                          AND tenant_id = :tenant_id
                          AND dry_run = :dry_run
                          AND status IN (:prepared, :failed)
                        """
                    ),
                    {
                        "completed": EFFECT_STATUS_COMPLETED,
                        "claim_owner": worker,
                        "now": timestamp,
                        "idempotency_key": key,
                        "tenant_id": tenant,
                        "dry_run": True,
                        "prepared": EFFECT_STATUS_PREPARED,
                        "failed": EFFECT_STATUS_FAILED,
                    },
                )
            else:
                result = await db.execute(
                    text(
                        f"""
                        UPDATE {self._table_name}
                        SET status = :running,
                            claim_owner = :claim_owner,
                            lease_expires_at = :lease_expires_at,
                            attempt = attempt + 1,
                            last_error = '',
                            started_at = :now,
                            completed_at = NULL,
                            failed_at = NULL,
                            updated_at = :now
                        WHERE idempotency_key = :idempotency_key
                          AND tenant_id = :tenant_id
                          AND dry_run = :dry_run
                          AND (
                              status IN (:prepared, :failed)
                              OR (
                                  status = :running
                                  AND (
                                      lease_expires_at IS NULL
                                      OR lease_expires_at <= :now
                                  )
                              )
                          )
                        """
                    ),
                    {
                        "running": EFFECT_STATUS_RUNNING,
                        "claim_owner": worker,
                        "lease_expires_at": lease,
                        "now": timestamp,
                        "idempotency_key": key,
                        "tenant_id": tenant,
                        "dry_run": False,
                        "prepared": EFFECT_STATUS_PREPARED,
                        "failed": EFFECT_STATUS_FAILED,
                    },
                )
            changed = int(getattr(result, "rowcount", 0) or 0) > 0
            existing = await self._fetch_existing(
                db,
                tenant,
                key,
                dry_run=dry_run,
            )

        if existing is None:
            raise RuntimeError("effect_claim_missing_after_update")
        record = _row_to_record(existing)
        if changed:
            if dry_run:
                return _record_with_status(record, EFFECT_STATUS_DRY_RUN)
            return record
        if record.status == EFFECT_STATUS_COMPLETED:
            return _record_with_status(record, EFFECT_STATUS_DUPLICATE)
        raise EffectClaimUnavailable(record)

    async def complete(
        self,
        *,
        idempotency_key: str,
        tenant_id: str,
        claim_owner: str,
        attempt: int,
        now: datetime | None = None,
    ) -> EffectCommitRecord:
        """CAS ``running`` to ``completed`` using owner and attempt fencing."""

        return await self._finalize(
            idempotency_key=idempotency_key,
            tenant_id=tenant_id,
            claim_owner=claim_owner,
            attempt=attempt,
            target_status=EFFECT_STATUS_COMPLETED,
            error="",
            now=now,
        )

    async def fail(
        self,
        *,
        idempotency_key: str,
        tenant_id: str,
        claim_owner: str,
        attempt: int,
        error: str,
        now: datetime | None = None,
    ) -> EffectCommitRecord:
        """CAS ``running`` to ``failed`` so a later worker can retry."""

        return await self._finalize(
            idempotency_key=idempotency_key,
            tenant_id=tenant_id,
            claim_owner=claim_owner,
            attempt=attempt,
            target_status=EFFECT_STATUS_FAILED,
            error=str(error or "effect_execution_failed")[:2048],
            now=now,
        )

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
    ) -> EffectCommitRecord:
        """Compatibility one-shot record implemented through the lifecycle API."""

        target_status = str(status or EFFECT_STATUS_RECORDED)
        record = await self.claim(
            idempotency_key=idempotency_key,
            tenant_id=tenant_id,
            session_id=session_id,
            trace_id=trace_id,
            owner=owner,
            type=type,
            claim_owner=f"compat-record-{secrets.token_hex(12)}",
            lease_seconds=60,
            payload=payload,
            dry_run=dry_run,
        )
        if record.status in {EFFECT_STATUS_DUPLICATE, EFFECT_STATUS_DRY_RUN}:
            return record
        if target_status == EFFECT_STATUS_RUNNING:
            return record
        if target_status == EFFECT_STATUS_FAILED:
            return await self.fail(
                idempotency_key=record.idempotency_key,
                tenant_id=record.tenant_id,
                claim_owner=record.claim_owner,
                attempt=record.attempt,
                error="compatibility_record_failed",
            )
        completed = await self.complete(
            idempotency_key=record.idempotency_key,
            tenant_id=record.tenant_id,
            claim_owner=record.claim_owner,
            attempt=record.attempt,
        )
        visible_status = (
            EFFECT_STATUS_COMPLETED
            if target_status == EFFECT_STATUS_COMPLETED
            else EFFECT_STATUS_RECORDED
        )
        return _record_with_status(completed, visible_status)

    async def list_recent(
        self,
        *,
        limit: int = 50,
        tenant_id: str = "",
        session_id: str = "",
        trace_id: str = "",
        owner: str = "",
        type: str = "",
        status: str = "",
        dry_run: bool | None = None,
        include_payload: bool = False,
    ) -> list[dict[str, Any]]:
        """Return recent rows while mapping completed states to legacy labels."""

        safe_limit = min(max(int(limit or 50), 1), 200)
        where_sql, params = _effect_log_filter_sql(
            tenant_id=tenant_id,
            session_id=session_id,
            trace_id=trace_id,
            owner=owner,
            type=type,
            status=status,
            dry_run=dry_run,
        )
        params["limit"] = safe_limit

        async with self._db() as db:
            result = await db.execute(
                text(
                    f"""
                    SELECT id, idempotency_key, tenant_id, session_id, trace_id,
                           owner, type, status, dry_run, payload, claim_owner,
                           lease_expires_at, attempt, last_error, created_at,
                           updated_at, started_at, completed_at, failed_at
                    FROM {self._table_name}
                    {where_sql}
                    ORDER BY id DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
            return [
                _effect_log_row_to_dict(row, include_payload=include_payload)
                for row in result.mappings().all()
            ]

    async def summarize(
        self,
        *,
        tenant_id: str = "",
        session_id: str = "",
        trace_id: str = "",
        owner: str = "",
        type: str = "",
        status: str = "",
        dry_run: bool | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Aggregate rows with both compatibility and lifecycle status views."""

        safe_limit = min(max(int(limit or 50), 1), 200)
        where_sql, params = _effect_log_filter_sql(
            tenant_id=tenant_id,
            session_id=session_id,
            trace_id=trace_id,
            owner=owner,
            type=type,
            status=status,
            dry_run=dry_run,
        )
        async with self._db() as db:
            total = await db.scalar(
                text(f"SELECT count(*) FROM {self._table_name} {where_sql}"),
                params,
            )
            lifecycle_status = await _effect_log_count_rows(
                db,
                table_name=self._table_name,
                where_sql=where_sql,
                params=params,
                group_columns=("status", "dry_run"),
                limit=safe_limit,
            )
            by_owner = await _effect_log_count_rows(
                db,
                table_name=self._table_name,
                where_sql=where_sql,
                params=params,
                group_columns=("owner",),
                limit=safe_limit,
            )
            by_type = await _effect_log_count_rows(
                db,
                table_name=self._table_name,
                where_sql=where_sql,
                params=params,
                group_columns=("type",),
                limit=safe_limit,
            )
            by_dry_run = await _effect_log_count_rows(
                db,
                table_name=self._table_name,
                where_sql=where_sql,
                params=params,
                group_columns=("dry_run",),
                limit=safe_limit,
            )
            matrix = await _effect_log_count_rows(
                db,
                table_name=self._table_name,
                where_sql=where_sql,
                params=params,
                group_columns=("owner", "type", "status", "dry_run"),
                limit=safe_limit,
            )
        return {
            "total": int(total or 0),
            "by_status": _compat_status_counts(lifecycle_status),
            "by_lifecycle_status": _lifecycle_status_counts(lifecycle_status),
            "by_owner": by_owner,
            "by_type": by_type,
            "by_dry_run": by_dry_run,
            "matrix": _compat_matrix(matrix),
        }

    async def _finalize(
        self,
        *,
        idempotency_key: str,
        tenant_id: str,
        claim_owner: str,
        attempt: int,
        target_status: str,
        error: str,
        now: datetime | None,
    ) -> EffectCommitRecord:
        if target_status not in {EFFECT_STATUS_COMPLETED, EFFECT_STATUS_FAILED}:
            raise ValueError(f"unsupported final effect status: {target_status}")
        key = _required(idempotency_key, "idempotency_key")
        tenant = str(tenant_id or "").strip()
        worker = _required(claim_owner, "claim_owner")
        expected_attempt = max(1, int(attempt))
        finalized_at = _as_utc(now or self._clock())
        timestamp_column = (
            "completed_at" if target_status == EFFECT_STATUS_COMPLETED else "failed_at"
        )

        async with self._db() as db:
            dialect = _dialect_name(db)
            result = await db.execute(
                text(
                    f"""
                    UPDATE {self._table_name}
                    SET status = :target_status,
                        lease_expires_at = NULL,
                        last_error = :last_error,
                        {timestamp_column} = :now,
                        updated_at = :now
                    WHERE idempotency_key = :idempotency_key
                      AND tenant_id = :tenant_id
                      AND dry_run = :dry_run
                      AND status = :running
                      AND claim_owner = :claim_owner
                      AND attempt = :attempt
                    """
                ),
                {
                    "target_status": target_status,
                    "last_error": error,
                    "now": _timestamp_param(dialect, finalized_at),
                    "idempotency_key": key,
                    "tenant_id": tenant,
                    "dry_run": False,
                    "running": EFFECT_STATUS_RUNNING,
                    "claim_owner": worker,
                    "attempt": expected_attempt,
                },
            )
            changed = int(getattr(result, "rowcount", 0) or 0) > 0
            existing = await self._fetch_existing(
                db,
                tenant,
                key,
                dry_run=False,
            )

        if existing is None:
            raise EffectClaimLost()
        record = _row_to_record(existing)
        if changed:
            return record
        if (
            record.status == target_status
            and record.claim_owner == worker
            and record.attempt == expected_attempt
        ):
            return record
        raise EffectClaimLost(record)

    async def _validate_schema(self, *, check_revision: bool) -> None:
        async with self._db() as db:
            if check_revision:
                try:
                    result = await db.execute(text("SELECT version_num FROM alembic_version"))
                except Exception as exc:
                    raise EffectLogSchemaError(
                        "alembic_version is unavailable; run `alembic upgrade head`"
                    ) from exc
                revisions = {str(value or "") for value in result.scalars().all()}
                if not any(
                    _revision_satisfies(current, self._required_revision) for current in revisions
                ):
                    current = ",".join(sorted(revisions)) or "missing"
                    raise EffectLogSchemaError(
                        "flow_effect_log migration is stale: "
                        f"required={self._required_revision}, current={current}"
                    )

            connection = await db.connection()
            structure = await connection.run_sync(
                lambda sync_connection: _inspect_structure(
                    sync_connection,
                    self._table_name,
                )
            )

        if not structure["exists"]:
            raise EffectLogSchemaError(f"{self._table_name} is missing; run `alembic upgrade head`")
        columns = set(structure["columns"])
        missing = sorted(_REQUIRED_COLUMNS - columns)
        if missing:
            raise EffectLogSchemaError(
                f"{self._table_name} is missing columns: {', '.join(missing)}"
            )
        unique_sets = {frozenset(values) for values in structure["unique_columns"]}
        if frozenset({"tenant_id", "idempotency_key", "dry_run"}) not in unique_sets:
            raise EffectLogSchemaError(
                f"{self._table_name} is missing unique(tenant_id, idempotency_key, dry_run)"
            )
        check_sql = " ".join(str(value or "").lower() for value in structure["checks"])
        if not all(status in check_sql for status in EFFECT_LIFECYCLE_STATUSES):
            raise EffectLogSchemaError(
                f"{self._table_name} is missing the lifecycle status constraint"
            )

    @asynccontextmanager
    async def _db(self) -> AsyncIterator[AsyncSession]:
        if self._session_scope is not None:
            async with self._session_scope() as db:
                yield db
            return

        factory = self._session_factory
        if factory is None:
            from app.infra.db import get_session_factory

            factory = get_session_factory()

        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def _fetch_existing(
        self,
        db: AsyncSession,
        tenant_id: str,
        idempotency_key: str,
        *,
        dry_run: bool,
    ) -> dict[str, Any] | None:
        result = await db.execute(
            text(
                f"""
                SELECT idempotency_key, tenant_id, owner, type, payload, status, dry_run,
                       claim_owner, lease_expires_at, attempt, last_error
                FROM {self._table_name}
                WHERE idempotency_key = :idempotency_key
                  AND tenant_id = :tenant_id
                  AND dry_run = :dry_run
                """
            ),
            {
                "idempotency_key": idempotency_key,
                "tenant_id": str(tenant_id or "").strip(),
                "dry_run": bool(dry_run),
            },
        )
        row = result.mappings().first()
        return dict(row) if row is not None else None

    def _insert_prepared_sql(self, dialect: str) -> str:
        payload_expression = "CAST(:payload AS JSONB)" if dialect == "postgresql" else ":payload"
        return f"""
            INSERT INTO {self._table_name} (
                idempotency_key, tenant_id, session_id, trace_id, owner, type,
                status, dry_run, payload, created_at, updated_at
            )
            VALUES (
                :idempotency_key, :tenant_id, :session_id, :trace_id, :owner, :type,
                :status, :dry_run, {payload_expression}, :now, :now
            )
            ON CONFLICT (tenant_id, idempotency_key, dry_run) DO NOTHING
        """

def _dialect_name(db: AsyncSession) -> str:
    bind = db.get_bind()
    return str(bind.dialect.name)


def _inspect_structure(connection: Connection, table_name: str) -> _TableStructure:
    inspector = inspect(connection)
    if not inspector.has_table(table_name):
        return {"exists": False, "columns": [], "unique_columns": [], "checks": []}
    unique_columns = [
        tuple(str(value) for value in constraint.get("column_names") or [])
        for constraint in inspector.get_unique_constraints(table_name)
    ]
    unique_columns.extend(
        tuple(str(value) for value in index.get("column_names") or [])
        for index in inspector.get_indexes(table_name)
        if index.get("unique")
    )
    return {
        "exists": True,
        "columns": [str(column["name"]) for column in inspector.get_columns(table_name)],
        "unique_columns": unique_columns,
        "checks": [
            str(constraint.get("sqltext") or "")
            for constraint in inspector.get_check_constraints(table_name)
        ],
    }


def _validate_identifier(value: str) -> str:
    identifier = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(identifier):
        raise ValueError(f"invalid SQL identifier: {value!r}")
    return identifier


def _revision_satisfies(current: str, required: str) -> bool:
    if current == required:
        return True
    current_match = re.match(r"^(\d+)(?:_|$)", current)
    required_match = re.match(r"^(\d+)(?:_|$)", required)
    if current_match is None or required_match is None:
        return False
    return int(current_match.group(1)) >= int(required_match.group(1))


def _required(value: str, name: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{name} cannot be empty")
    return cleaned


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp_param(dialect: str, value: datetime) -> datetime | str:
    return value.isoformat() if dialect == "sqlite" else value


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _decode_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def _row_to_record(row: dict[str, Any]) -> EffectCommitRecord:
    return EffectCommitRecord(
        type=str(row.get("type") or ""),
        owner=str(row.get("owner") or ""),
        idempotency_key=str(row.get("idempotency_key") or ""),
        payload=_decode_payload(row.get("payload")),
        status=str(row.get("status") or ""),
        error=str(row.get("last_error") or ""),
        dry_run=bool(row.get("dry_run")),
        tenant_id=str(row.get("tenant_id") or ""),
        claim_owner=str(row.get("claim_owner") or ""),
        lease_expires_at=_timestamp_to_string(row.get("lease_expires_at")),
        attempt=int(row.get("attempt") or 0),
    )


def _record_with_status(record: EffectCommitRecord, status: str) -> EffectCommitRecord:
    return EffectCommitRecord(
        type=record.type,
        owner=record.owner,
        idempotency_key=record.idempotency_key,
        payload=dict(record.payload),
        status=status,
        error=record.error,
        dry_run=record.dry_run,
        tenant_id=record.tenant_id,
        claim_owner=record.claim_owner,
        lease_expires_at=record.lease_expires_at,
        attempt=record.attempt,
    )


def _effect_log_filter_sql(
    *,
    tenant_id: str = "",
    session_id: str = "",
    trace_id: str = "",
    owner: str = "",
    type: str = "",
    status: str = "",
    dry_run: bool | None = None,
) -> tuple[str, dict[str, Any]]:
    filters: list[str] = []
    params: dict[str, Any] = {}
    for column, value in {
        "tenant_id": tenant_id,
        "session_id": session_id,
        "trace_id": trace_id,
        "owner": owner,
        "type": type,
    }.items():
        cleaned = str(value or "").strip()
        if cleaned:
            filters.append(f"{column} = :{column}")
            params[column] = cleaned

    requested_status = str(status or "").strip().lower()
    if requested_status == EFFECT_STATUS_RECORDED:
        filters.extend(["status = :status", "dry_run = :status_dry_run"])
        params.update(status=EFFECT_STATUS_COMPLETED, status_dry_run=False)
    elif requested_status == EFFECT_STATUS_DRY_RUN:
        filters.extend(["status = :status", "dry_run = :status_dry_run"])
        params.update(status=EFFECT_STATUS_COMPLETED, status_dry_run=True)
    elif requested_status == EFFECT_STATUS_DUPLICATE:
        filters.append("1 = 0")
    elif requested_status:
        filters.append("status = :status")
        params["status"] = requested_status
    if dry_run is not None:
        filters.append("dry_run = :dry_run")
        params["dry_run"] = bool(dry_run)
    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    return where_sql, params


async def _effect_log_count_rows(
    db: AsyncSession,
    *,
    table_name: str,
    where_sql: str,
    params: dict[str, Any],
    group_columns: tuple[str, ...],
    limit: int,
) -> list[dict[str, Any]]:
    columns = ", ".join(group_columns)
    result = await db.execute(
        text(
            f"""
            SELECT {columns}, count(*) AS count
            FROM {table_name}
            {where_sql}
            GROUP BY {columns}
            ORDER BY count DESC
            LIMIT :limit
            """
        ),
        {**params, "limit": limit},
    )
    rows: list[dict[str, Any]] = []
    for row in result.mappings().all():
        item = {column: _effect_log_scalar(row[column]) for column in group_columns}
        item["count"] = int(row["count"] or 0)
        rows.append(item)
    return rows


def _effect_log_scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value) if value in {0, 1} else value
    return str(value or "")


def _compat_status(status: str, *, dry_run: bool) -> str:
    if status == EFFECT_STATUS_COMPLETED:
        return EFFECT_STATUS_DRY_RUN if dry_run else EFFECT_STATUS_RECORDED
    return status


def _compat_status_counts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[str, int] = {}
    for row in rows:
        label = _compat_status(str(row.get("status") or ""), dry_run=bool(row.get("dry_run")))
        totals[label] = totals.get(label, 0) + int(row.get("count") or 0)
    return [
        {"status": status, "count": count}
        for status, count in sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    ]


def _lifecycle_status_counts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "")
        totals[status] = totals.get(status, 0) + int(row.get("count") or 0)
    return [
        {"status": status, "count": count}
        for status, count in sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    ]


def _compat_matrix(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[tuple[str, str, str, bool], int] = {}
    for row in rows:
        dry_run = bool(row.get("dry_run"))
        key = (
            str(row.get("owner") or ""),
            str(row.get("type") or ""),
            _compat_status(str(row.get("status") or ""), dry_run=dry_run),
            dry_run,
        )
        totals[key] = totals.get(key, 0) + int(row.get("count") or 0)
    return [
        {
            "owner": owner,
            "type": effect_type,
            "status": status,
            "dry_run": dry_run,
            "count": count,
        }
        for (owner, effect_type, status, dry_run), count in sorted(
            totals.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]


def _effect_log_row_to_dict(row: Any, *, include_payload: bool) -> dict[str, Any]:
    payload = _decode_payload(row.get("payload"))
    lifecycle_status = str(row["status"] or "")
    dry_run = bool(row["dry_run"])
    item: dict[str, Any] = {
        "id": int(row["id"]),
        "idempotency_key": str(row["idempotency_key"] or ""),
        "tenant_id": str(row["tenant_id"] or ""),
        "session_id": str(row["session_id"] or ""),
        "trace_id": str(row["trace_id"] or ""),
        "owner": str(row["owner"] or ""),
        "type": str(row["type"] or ""),
        "status": _compat_status(lifecycle_status, dry_run=dry_run),
        "lifecycle_status": lifecycle_status,
        "dry_run": dry_run,
        "claim_owner": str(row["claim_owner"] or ""),
        "lease_expires_at": _timestamp_to_string(row.get("lease_expires_at")),
        "attempt": int(row["attempt"] or 0),
        "has_error": bool(row.get("last_error")),
        "payload_keys": sorted(str(key) for key in payload),
        "payload_size": len(_json_dumps(payload)),
        "created_at": _timestamp_to_string(row.get("created_at")),
        "updated_at": _timestamp_to_string(row.get("updated_at")),
        "started_at": _timestamp_to_string(row.get("started_at")),
        "completed_at": _timestamp_to_string(row.get("completed_at")),
        "failed_at": _timestamp_to_string(row.get("failed_at")),
    }
    if include_payload:
        item["payload"] = payload
    return item


def _timestamp_to_string(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")
