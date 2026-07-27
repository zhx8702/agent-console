"""Durable channel-connection contracts, validation, and CAS persistence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import NAMESPACE_URL, uuid5

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.admin.mutation_ledger import (
    MutationAudit,
    MutationChange,
    MutationIdempotencyConflictError,
    MutationIdentity,
    MutationLedgerError,
    MutationOutcome,
    fingerprint,
    hash_identifier,
    plugin_admin_mutation_idempotency,
    run_idempotent_mutation,
)
from app.channel.adapters import (
    CHANNEL_ADAPTER_PROBE_TIMEOUT_MAX_SECONDS,
    WECHAT_SDK_ADAPTER_ID,
    ChannelAdapterCatalog,
    ChannelAdapterDescriptor,
    ChannelProbeResult,
)
from app.channel.identity import LEGACY_WXBOT_CONNECTION_ID
from app.models.channel_connection import ChannelConnectionRow

_RUNTIME_READY_REFRESH_INTERVAL = timedelta(minutes=1)
_CONNECTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SECRET_REF = re.compile(r"^(?P<scheme>[a-z][a-z0-9-]*):(?://)?(?P<locator>.+)$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SAFE_STATUS = re.compile(r"[^a-z0-9_.-]+")
_SENSITIVE_PARTS = frozenset(
    {
        "authorization",
        "credential",
        "credentials",
        "password",
        "privatekey",
        "secret",
        "token",
        "apikey",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
    }
)


class _ConnectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChannelConnectionCreateRequest(_ConnectionModel):
    connection_id: str | None = Field(default=None, min_length=1, max_length=64)
    adapter_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    config_json: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("config_json", "config"),
    )
    secret_ref: str = Field(default="", max_length=512)
    desired_state: Literal["draft", "disabled"] = "draft"
    priority: int = Field(default=100, ge=0, le=100_000)
    required_for_launch: bool = False

    @field_validator("connection_id")
    @classmethod
    def validate_connection_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_connection_id(value)

    @field_validator("adapter_id")
    @classmethod
    def normalize_adapter_id(cls, value: str) -> str:
        return str(value or "").strip().lower()

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("display_name cannot be blank")
        return normalized


class ChannelConnectionUpdateRequest(_ConnectionModel):
    adapter_id: str | None = Field(default=None, min_length=1, max_length=64)
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    config_json: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("config_json", "config"),
    )
    secret_ref: str | None = Field(default=None, max_length=512)
    priority: int | None = Field(default=None, ge=0, le=100_000)
    required_for_launch: bool | None = None
    desired_state: Literal["draft", "disabled", "enabled"] | None = None

    @field_validator("display_name")
    @classmethod
    def normalize_optional_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("display_name cannot be blank")
        return normalized

    @model_validator(mode="after")
    def require_update(self) -> ChannelConnectionUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("at least one mutable field is required")
        return self


class ChannelConnectionDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    tenant_id: str
    connection_id: str
    adapter_id: str
    display_name: str
    desired_state: str
    effective_state: str
    config_json: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str = ""
    secret_status: str
    secret_fingerprint: str = ""
    version: int = Field(ge=1)
    priority: int = Field(ge=0, le=100_000)
    required_for_launch: bool
    last_probed_at: datetime | None = None
    last_probe_status: str = ""
    last_error_code: str = ""
    last_inbound_at: datetime | None = None
    last_outbound_delivered_at: datetime | None = None
    managed_by: str = "console"
    read_only: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ChannelConnectionCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    status: str
    error_codes: list[str] = Field(default_factory=list)
    connection: ChannelConnectionDocument


class ChannelConnectionDeleteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    connection_id: str
    deleted: bool = True
    version: int


@dataclass(frozen=True, slots=True)
class ChannelConnectionMutationResult:
    value: ChannelConnectionDocument | ChannelConnectionCheckResult | ChannelConnectionDeleteResult
    status_code: int
    replayed: bool
    mutation_id: str


class ChannelConnectionError(RuntimeError):
    pass


class ChannelConnectionNotFoundError(ChannelConnectionError):
    pass


class ChannelConnectionExistsError(ChannelConnectionError):
    pass


class ChannelConnectionReadOnlyError(ChannelConnectionError):
    pass


class ChannelConnectionVersionConflictError(ChannelConnectionError):
    def __init__(self, *, expected: int, current: int) -> None:
        super().__init__(f"expected version {expected}, current version {current}")
        self.expected = expected
        self.current = current


class ChannelConnectionStateError(ChannelConnectionError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ChannelConnectionPayloadError(ChannelConnectionError):
    def __init__(self, codes: list[str] | tuple[str, ...]) -> None:
        normalized = tuple(dict.fromkeys(str(code) for code in codes if str(code)))
        super().__init__(normalized[0] if normalized else "invalid_channel_connection")
        self.codes = normalized or ("invalid_channel_connection",)


def generated_connection_id(tenant_id: str, idempotency_key: str) -> str:
    """Derive a replay-stable UUID when the caller omits a connection ID."""

    return str(
        uuid5(
            NAMESPACE_URL,
            f"agent-console:channel-connection:{tenant_id}:{idempotency_key}",
        )
    )


def legacy_wxbot_connection_from_settings(
    settings: Any,
    *,
    tenant_id: str | None = None,
) -> ChannelConnectionDocument:
    """Project legacy WXBOT_* settings as a read-only synthetic connection."""

    tenant = str(tenant_id or getattr(settings, "wxbot_default_tenant_id", "") or "default").strip()
    config: dict[str, Any] = {
        "sdk_url": str(getattr(settings, "wxbot_sdk_url", "http://127.0.0.1:5080") or "").strip(),
        "poll_interval_seconds": float(getattr(settings, "wxbot_bridge_poll_interval", 3.0) or 3.0),
        "send_interval_seconds": float(getattr(settings, "wxbot_bridge_send_interval", 2.0) or 2.0),
    }
    media_base_url = str(getattr(settings, "wxbot_media_base_url", "") or "").strip()
    if media_base_url:
        config["media_base_url"] = media_base_url
    return ChannelConnectionDocument(
        tenant_id=tenant,
        connection_id=LEGACY_WXBOT_CONNECTION_ID,
        adapter_id=WECHAT_SDK_ADAPTER_ID,
        display_name="Legacy WeChat connection",
        desired_state="enabled",
        effective_state="unverified",
        config_json=config,
        secret_ref="",
        secret_status="not_required",
        secret_fingerprint="",
        version=1,
        priority=100,
        required_for_launch=False,
        managed_by="environment",
        read_only=True,
    )


class ChannelConnectionStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        catalog: ChannelAdapterCatalog,
        *,
        probe_timeout_seconds: float = 10.0,
    ) -> None:
        timeout = float(probe_timeout_seconds)
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or timeout > CHANNEL_ADAPTER_PROBE_TIMEOUT_MAX_SECONDS
        ):
            raise ValueError(
                "probe timeout must be positive and no greater than "
                f"{CHANNEL_ADAPTER_PROBE_TIMEOUT_MAX_SECONDS:g} seconds"
            )
        self._sessions = session_factory
        self.catalog = catalog
        self._probe_timeout_seconds = timeout

    async def list(self, tenant_id: str) -> list[ChannelConnectionDocument]:
        async with self._sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(ChannelConnectionRow)
                        .where(ChannelConnectionRow.tenant_id == tenant_id)
                        .order_by(
                            ChannelConnectionRow.priority.asc(),
                            ChannelConnectionRow.display_name.asc(),
                            ChannelConnectionRow.connection_id.asc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [_document_from_row(row) for row in rows]

    async def get(
        self,
        tenant_id: str,
        connection_id: str,
    ) -> ChannelConnectionDocument:
        if connection_id == LEGACY_WXBOT_CONNECTION_ID:
            raise ChannelConnectionReadOnlyError("environment_managed_connection")
        async with self._sessions() as session:
            row = await session.get(
                ChannelConnectionRow,
                {"tenant_id": tenant_id, "connection_id": connection_id},
            )
        if row is None:
            raise ChannelConnectionNotFoundError(connection_id)
        return _document_from_row(row)

    async def create(
        self,
        tenant_id: str,
        request: ChannelConnectionCreateRequest,
        *,
        idempotency_key: str,
        audit: MutationAudit,
    ) -> ChannelConnectionMutationResult:
        connection_id = request.connection_id or generated_connection_id(tenant_id, idempotency_key)
        if connection_id == LEGACY_WXBOT_CONNECTION_ID:
            raise ChannelConnectionReadOnlyError("reserved_legacy_connection_id")
        registration = self.catalog.get(request.adapter_id)
        if registration is None:
            raise ChannelConnectionPayloadError(["adapter_not_registered"])
        secret_ref = normalize_secret_ref(request.secret_ref)
        secret_errors = _validate_provided_secret_ref(
            secret_ref,
            registration.descriptor,
        )
        if secret_errors:
            raise ChannelConnectionPayloadError(secret_errors)
        config_json = _normalize_adapter_config(
            registration.descriptor.adapter_id,
            request.config_json,
        )
        _assert_non_sensitive_config(config_json)
        config_errors = _validate_provided_config(
            config_json,
            registration.descriptor,
        )
        if config_errors:
            raise ChannelConnectionPayloadError(config_errors)
        now = datetime.now(UTC)
        document = ChannelConnectionDocument(
            tenant_id=tenant_id,
            connection_id=connection_id,
            adapter_id=registration.descriptor.adapter_id,
            display_name=request.display_name.strip(),
            desired_state=request.desired_state,
            effective_state="unverified",
            config_json=config_json,
            secret_ref=secret_ref,
            secret_status=_secret_status(secret_ref, registration.descriptor),
            secret_fingerprint=_secret_ref_fingerprint(secret_ref),
            version=1,
            priority=request.priority,
            required_for_launch=request.required_for_launch,
            created_at=now,
            updated_at=now,
        )

        async def mutate(conn: AsyncConnection) -> MutationChange:
            await conn.execute(
                insert(ChannelConnectionRow.__table__).values(
                    **_persistence_values(document),
                    created_at=now,
                    updated_at=now,
                )
            )
            return MutationChange(
                response=document,
                before_state={"exists": False, "version": 0},
                after_state=_audit_state(document),
                resource_version="1",
                status_code=201,
            )

        try:
            outcome = await self._run_mutation(
                identity=MutationIdentity(
                    tenant_id=tenant_id,
                    plugin_name="channel_connections",
                    operation="channel.connection.create",
                    resource_key=connection_id,
                    idempotency_key=idempotency_key,
                    request_payload={
                        "tenant_id": tenant_id,
                        **request.model_dump(
                            mode="json",
                            exclude={"connection_id", "secret_ref"},
                        ),
                        "connection_id": connection_id,
                        "secret_ref": secret_ref,
                    },
                ),
                audit=audit,
                mutate=mutate,
            )
        except IntegrityError as exc:
            raise ChannelConnectionExistsError(connection_id) from exc
        return _document_outcome(outcome)

    async def update(
        self,
        tenant_id: str,
        connection_id: str,
        request: ChannelConnectionUpdateRequest,
        *,
        expected_version: int,
        idempotency_key: str,
        audit: MutationAudit,
    ) -> ChannelConnectionMutationResult:
        _reject_legacy(connection_id)
        updates = request.model_dump(exclude_unset=True)
        if "config_json" in updates:
            updates["config_json"] = dict(updates.get("config_json") or {})
            _assert_non_sensitive_config(updates["config_json"])
        if "secret_ref" in updates:
            updates["secret_ref"] = normalize_secret_ref(updates.get("secret_ref") or "")

        async def mutate(conn: AsyncConnection) -> MutationChange:
            before = await _get_document(conn, tenant_id, connection_id, for_update=True)
            _require_version(before, expected_version)
            requested_adapter = str(updates.pop("adapter_id", "") or "").strip().lower()
            if requested_adapter and requested_adapter != before.adapter_id:
                raise ChannelConnectionPayloadError(["adapter_id_is_immutable"])
            requested_desired = str(updates.pop("desired_state", "") or "").strip()
            if requested_desired and requested_desired != before.desired_state:
                raise ChannelConnectionStateError("desired_state_update_requires_action")
            if "config_json" in updates:
                updates["config_json"] = _normalize_adapter_config(
                    before.adapter_id,
                    updates["config_json"],
                )
                registration = self.catalog.get(before.adapter_id)
                if registration is None:
                    raise ChannelConnectionPayloadError(["adapter_not_registered"])
                config_errors = _validate_provided_config(
                    updates["config_json"],
                    registration.descriptor,
                )
                if config_errors:
                    raise ChannelConnectionPayloadError(config_errors)
            if updates.get("secret_ref"):
                registration = self.catalog.get(before.adapter_id)
                if registration is None:
                    raise ChannelConnectionPayloadError(["adapter_not_registered"])
                secret_errors = _validate_provided_secret_ref(
                    str(updates["secret_ref"]),
                    registration.descriptor,
                )
                if secret_errors:
                    raise ChannelConnectionPayloadError(secret_errors)
            values: dict[str, Any] = {
                key: value for key, value in updates.items() if key != "secret_ref"
            }
            if "display_name" in values:
                values["display_name"] = str(values["display_name"]).strip()
            if "secret_ref" in updates:
                secret_ref = str(updates["secret_ref"])
                registration = self.catalog.get(before.adapter_id)
                if registration is None:
                    raise ChannelConnectionPayloadError(["adapter_not_registered"])
                values.update(
                    secret_ref=secret_ref,
                    secret_status=_secret_status(secret_ref, registration.descriptor),
                    secret_fingerprint=_secret_ref_fingerprint(secret_ref),
                )
            if "config_json" in updates or "secret_ref" in updates:
                values.update(
                    effective_state="unverified",
                    last_probe_status="",
                    last_error_code="",
                    last_probed_at=None,
                    last_inbound_at=None,
                    last_outbound_delivered_at=None,
                )
            if before.desired_state == "enabled":
                candidate = before.model_copy(update=values)
                validation_errors = validate_connection_document(candidate, self.catalog)
                if validation_errors:
                    raise ChannelConnectionStateError(validation_errors[0])
            after = await _cas_update(
                conn,
                before,
                expected_version=expected_version,
                values=values,
            )
            return MutationChange(
                response=after,
                before_state=_audit_state(before),
                after_state=_audit_state(after),
                resource_version=str(after.version),
            )

        outcome = await self._run_mutation(
            identity=MutationIdentity(
                tenant_id=tenant_id,
                plugin_name="channel_connections",
                operation="channel.connection.update",
                resource_key=connection_id,
                idempotency_key=idempotency_key,
                request_payload={
                    "expected_version": expected_version,
                    "updates": updates,
                },
            ),
            audit=audit,
            mutate=mutate,
        )
        return _document_outcome(outcome)

    async def validate(
        self,
        tenant_id: str,
        connection_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        audit: MutationAudit,
    ) -> ChannelConnectionMutationResult:
        _reject_legacy(connection_id)

        async def mutate(conn: AsyncConnection) -> MutationChange:
            before = await _get_document(conn, tenant_id, connection_id, for_update=True)
            _require_version(before, expected_version)
            errors = validate_connection_document(before, self.catalog)
            # Validation is a configuration check, not runtime telemetry.
            # Preserve effective/probe state in both success and failure
            # cases; the result and idempotency record carry this check.
            values: dict[str, Any] = {}
            after = await _cas_update(
                conn,
                before,
                expected_version=expected_version,
                values=values,
            )
            result = ChannelConnectionCheckResult(
                ok=not errors,
                status="valid" if not errors else "invalid",
                error_codes=list(errors),
                connection=after,
            )
            return MutationChange(
                response=result,
                before_state=_audit_state(before),
                after_state=_audit_state(after),
                resource_version=str(after.version),
            )

        outcome = await self._run_connection_operation(
            tenant_id,
            connection_id,
            operation="validate",
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            audit=audit,
            mutate=mutate,
        )
        return _check_outcome(outcome)

    async def probe(
        self,
        tenant_id: str,
        connection_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        audit: MutationAudit,
    ) -> ChannelConnectionMutationResult:
        _reject_legacy(connection_id)

        identity = self._connection_operation_identity(
            tenant_id,
            connection_id,
            operation="probe",
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        replayed = await self._find_completed_mutation(identity)
        if replayed is not None:
            return _check_outcome(replayed)

        # Probe plugins may perform slow network or SDK I/O. Snapshot and
        # validate before invoking them, with no database session/transaction
        # held. The write phase below re-checks the version under lock.
        probed_document = await self.get(tenant_id, connection_id)
        _require_version(probed_document, expected_version)
        registration_snapshot = self.catalog.get(probed_document.adapter_id)
        validation_errors = (
            ("adapter_not_registered",)
            if registration_snapshot is None
            else _validate_connection_against_descriptor(
                probed_document,
                registration_snapshot.descriptor,
            )
        )
        if validation_errors:
            probe = ChannelProbeResult(
                ok=False,
                status="validation_failed",
                error_code=validation_errors[0],
            )
            errors = validation_errors
        else:
            assert registration_snapshot is not None
            try:
                probe = await registration_snapshot.probe_connection(
                    probed_document,
                    timeout_seconds=self._probe_timeout_seconds,
                )
            except TimeoutError:
                probe = ChannelProbeResult(
                    ok=False,
                    status="timeout",
                    error_code="adapter_probe_timeout",
                )
            except Exception:
                probe = ChannelProbeResult(
                    ok=False,
                    status="failed",
                    error_code="adapter_probe_failed",
                )
            errors = (
                (_safe_code(probe.error_code) or "adapter_probe_failed",) if not probe.ok else ()
            )

        async def mutate(conn: AsyncConnection) -> MutationChange:
            before = await _get_document(conn, tenant_id, connection_id, for_update=True)
            _require_version(before, expected_version)
            current_registration = self.catalog.get(before.adapter_id)
            persisted_probe = probe
            persisted_errors = errors
            if current_registration is None:
                persisted_probe = ChannelProbeResult(
                    ok=False,
                    status="unavailable",
                    error_code="adapter_not_registered",
                )
                persisted_errors = ("adapter_not_registered",)
            elif (
                registration_snapshot is None
                or current_registration.descriptor != registration_snapshot.descriptor
            ):
                persisted_probe = ChannelProbeResult(
                    ok=False,
                    status="unavailable",
                    error_code="adapter_registration_changed",
                )
                persisted_errors = ("adapter_registration_changed",)
            elif registration_snapshot.probe is not None and current_registration.probe is None:
                persisted_probe = ChannelProbeResult(
                    ok=False,
                    status="unavailable",
                    error_code="adapter_probe_unavailable",
                )
                persisted_errors = ("adapter_probe_unavailable",)
            now = datetime.now(UTC)
            effective_state = (
                "enabled"
                if persisted_probe.ok and before.desired_state == "enabled"
                else "ready"
                if persisted_probe.ok
                else "error"
            )
            values = {
                "effective_state": effective_state,
                "last_probed_at": now,
                "last_probe_status": _safe_code(persisted_probe.status)
                or ("ok" if persisted_probe.ok else "failed"),
                "last_error_code": persisted_errors[0] if persisted_errors else "",
            }
            after = await _cas_update(
                conn,
                before,
                expected_version=expected_version,
                values=values,
            )
            result = ChannelConnectionCheckResult(
                ok=persisted_probe.ok,
                status=_safe_code(persisted_probe.status)
                or ("ok" if persisted_probe.ok else "failed"),
                error_codes=list(persisted_errors),
                connection=after,
            )
            return MutationChange(
                response=result,
                before_state=_audit_state(before),
                after_state=_audit_state(after),
                resource_version=str(after.version),
            )

        outcome = await self._run_mutation(
            identity=identity,
            audit=audit,
            mutate=mutate,
        )
        return _check_outcome(outcome)

    async def set_desired_state(
        self,
        tenant_id: str,
        connection_id: str,
        *,
        enabled: bool,
        expected_version: int,
        idempotency_key: str,
        audit: MutationAudit,
    ) -> ChannelConnectionMutationResult:
        _reject_legacy(connection_id)
        desired = "enabled" if enabled else "disabled"
        operation = "enable" if enabled else "disable"

        async def mutate(conn: AsyncConnection) -> MutationChange:
            before = await _get_document(conn, tenant_id, connection_id, for_update=True)
            _require_version(before, expected_version)
            if enabled:
                errors = validate_connection_document(before, self.catalog)
                if errors:
                    raise ChannelConnectionStateError(errors[0])
                registration = self.catalog.get(before.adapter_id)
                if registration is None or registration.provider_factory is None:
                    raise ChannelConnectionStateError("adapter_runtime_unavailable")
                values = {"desired_state": "enabled"}
            else:
                # This endpoint owns desired state only. A connector/reconciler
                # or a subsequent probe must confirm that effective state has
                # actually converged; reporting an immediate stop would lie.
                values = {"desired_state": "disabled"}
            after = await _cas_update(
                conn,
                before,
                expected_version=expected_version,
                values=values,
            )
            return MutationChange(
                response=after,
                before_state=_audit_state(before),
                after_state=_audit_state(after),
                resource_version=str(after.version),
            )

        outcome = await self._run_connection_operation(
            tenant_id,
            connection_id,
            operation=operation,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            audit=audit,
            mutate=mutate,
            extra_payload={"desired_state": desired},
        )
        return _document_outcome(outcome)

    async def mark_runtime_stopped(
        self,
        tenant_id: str,
        connection_id: str,
    ) -> bool:
        """Converge runtime telemetry after a connector has actually stopped.

        This is intentionally not a configuration mutation: it neither bumps
        the configuration version nor writes the admin mutation ledger. The
        conditional update prevents a late worker callback from marking a
        connection stopped after an operator has re-enabled it.
        """

        async with self._sessions() as session:
            async with session.begin():
                result = await session.execute(
                    update(ChannelConnectionRow)
                    .where(
                        ChannelConnectionRow.tenant_id == tenant_id,
                        ChannelConnectionRow.connection_id == connection_id,
                        ChannelConnectionRow.desired_state == "disabled",
                        ChannelConnectionRow.effective_state != "disabled",
                    )
                    .values(effective_state="disabled")
                )
        return int(result.rowcount or 0) == 1

    async def mark_runtime_ready(
        self,
        tenant_id: str,
        connection_id: str,
        *,
        binding_fingerprint: str,
    ) -> bool:
        """Converge managed connection telemetry after a live SDK probe.

        The bridge owns this runtime observation.  It must not advance the
        operator-facing configuration version, and a stale worker may not
        certify a connection whose endpoint or credential reference changed.
        """

        expected_fingerprint = str(binding_fingerprint or "").strip()
        if not expected_fingerprint:
            return False
        async with self._sessions() as session:
            async with session.begin():
                row = await session.get(
                    ChannelConnectionRow,
                    {"tenant_id": tenant_id, "connection_id": connection_id},
                    with_for_update=True,
                )
                if row is None or row.desired_state != "enabled":
                    return False
                document = _document_from_row(row)
                if connection_binding_fingerprint(document) != expected_fingerprint:
                    return False
                now = datetime.now(UTC)
                last_probed_at = row.last_probed_at
                if last_probed_at is not None and last_probed_at.tzinfo is None:
                    last_probed_at = last_probed_at.replace(tzinfo=UTC)
                if (
                    row.effective_state == "enabled"
                    and row.last_probe_status == "ready"
                    and not row.last_error_code
                    and last_probed_at is not None
                    and last_probed_at >= now - _RUNTIME_READY_REFRESH_INTERVAL
                ):
                    return True
                row.effective_state = "enabled"
                row.last_probed_at = now
                row.last_probe_status = "ready"
                row.last_error_code = ""
        return True

    async def record_runtime_activity(
        self,
        tenant_id: str,
        connection_id: str,
        *,
        direction: Literal["inbound", "outbound_delivered"],
        binding_fingerprint: str,
    ) -> bool:
        """Persist transport evidence without mutating configuration version.

        The binding fingerprint prevents a stale worker from certifying a
        connection after its endpoint or credential reference has changed.
        Only final platform delivery acknowledgements count as outbound
        evidence; queue insertion is deliberately insufficient.
        """

        if direction not in {"inbound", "outbound_delivered"}:
            raise ValueError("unsupported channel connection activity direction")
        expected_fingerprint = str(binding_fingerprint or "").strip()
        if not expected_fingerprint:
            return False
        async with self._sessions() as session:
            async with session.begin():
                row = await session.get(
                    ChannelConnectionRow,
                    {"tenant_id": tenant_id, "connection_id": connection_id},
                    with_for_update=True,
                )
                if row is None or row.desired_state != "enabled":
                    return False
                document = _document_from_row(row)
                if connection_binding_fingerprint(document) != expected_fingerprint:
                    return False
                now = datetime.now(UTC)
                if direction == "inbound":
                    row.last_inbound_at = now
                else:
                    row.last_outbound_delivered_at = now
        return True

    async def delete(
        self,
        tenant_id: str,
        connection_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        audit: MutationAudit,
    ) -> ChannelConnectionMutationResult:
        _reject_legacy(connection_id)

        async def mutate(conn: AsyncConnection) -> MutationChange:
            before = await _get_document(conn, tenant_id, connection_id, for_update=True)
            _require_version(before, expected_version)
            if before.desired_state == "enabled" or before.effective_state == "enabled":
                raise ChannelConnectionStateError(
                    "channel_connection_must_be_disabled_before_delete"
                )
            result = await conn.execute(
                delete(ChannelConnectionRow.__table__).where(
                    ChannelConnectionRow.tenant_id == tenant_id,
                    ChannelConnectionRow.connection_id == connection_id,
                    ChannelConnectionRow.version == expected_version,
                )
            )
            if int(result.rowcount or 0) != 1:
                await _raise_current_version(conn, before, expected_version)
            response = ChannelConnectionDeleteResult(
                tenant_id=tenant_id,
                connection_id=connection_id,
                version=expected_version,
            )
            return MutationChange(
                response=response,
                before_state=_audit_state(before),
                after_state={"exists": False, "version": expected_version},
                resource_version=str(expected_version),
            )

        outcome = await self._run_connection_operation(
            tenant_id,
            connection_id,
            operation="delete",
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            audit=audit,
            mutate=mutate,
        )
        return ChannelConnectionMutationResult(
            value=ChannelConnectionDeleteResult.model_validate(outcome.response),
            status_code=outcome.status_code,
            replayed=outcome.replayed,
            mutation_id=outcome.mutation_id,
        )

    async def _run_connection_operation(
        self,
        tenant_id: str,
        connection_id: str,
        *,
        operation: str,
        expected_version: int,
        idempotency_key: str,
        audit: MutationAudit,
        mutate: Any,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> MutationOutcome:
        identity = self._connection_operation_identity(
            tenant_id,
            connection_id,
            operation=operation,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            extra_payload=extra_payload,
        )
        return await self._run_mutation(
            identity=identity,
            audit=audit,
            mutate=mutate,
        )

    @staticmethod
    def _connection_operation_identity(
        tenant_id: str,
        connection_id: str,
        *,
        operation: str,
        expected_version: int,
        idempotency_key: str,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> MutationIdentity:
        return MutationIdentity(
            tenant_id=tenant_id,
            plugin_name="channel_connections",
            operation=f"channel.connection.{operation}",
            resource_key=connection_id,
            idempotency_key=idempotency_key,
            request_payload={
                "expected_version": expected_version,
                **dict(extra_payload or {}),
            },
        )

    async def _find_completed_mutation(
        self,
        identity: MutationIdentity,
    ) -> MutationOutcome | None:
        """Read an exact replay before a probe performs external I/O."""

        tenant_id = str(identity.tenant_id or "").strip()[:64]
        plugin_name = str(identity.plugin_name or "").strip()[:64]
        operation = str(identity.operation or "").strip()[:96]
        idempotency_key = str(identity.idempotency_key or "").strip()[:128]
        if not tenant_id or not plugin_name or not operation or not idempotency_key:
            raise MutationLedgerError(
                "tenant_id, plugin_name, operation and idempotency_key are required"
            )
        request_hash = fingerprint(identity.request_payload)
        resource_hash = hash_identifier(str(identity.resource_key or "").strip()[:512])
        async with self._sessions() as session:
            existing = (
                (
                    await session.execute(
                        select(plugin_admin_mutation_idempotency).where(
                            plugin_admin_mutation_idempotency.c.tenant_id == tenant_id,
                            plugin_admin_mutation_idempotency.c.plugin_name == plugin_name,
                            plugin_admin_mutation_idempotency.c.operation == operation,
                            plugin_admin_mutation_idempotency.c.idempotency_key_hash
                            == hash_identifier(idempotency_key),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if existing is None:
            return None
        if (
            str(existing["request_hash"]) != request_hash
            or str(existing["resource_key_hash"]) != resource_hash
        ):
            raise MutationIdempotencyConflictError("idempotency key was used for another request")
        if existing["completed_at"] is None:
            raise MutationLedgerError("idempotency mutation is incomplete")
        return MutationOutcome(
            response=existing["response_json"],
            status_code=int(existing["response_status_code"] or 200),
            replayed=True,
            mutation_id=str(existing["id"]),
        )

    async def _run_mutation(
        self,
        *,
        identity: MutationIdentity,
        audit: MutationAudit,
        mutate: Any,
    ) -> MutationOutcome:
        async with self._sessions() as session:
            async with session.begin():
                conn = await session.connection()

                async def callback() -> MutationChange:
                    return await mutate(conn)

                return await run_idempotent_mutation(
                    conn,
                    identity=identity,
                    audit=audit,
                    mutate=callback,
                )


def validate_connection_document(
    connection: ChannelConnectionDocument,
    catalog: ChannelAdapterCatalog,
) -> tuple[str, ...]:
    registration = catalog.get(connection.adapter_id)
    if registration is None:
        return ("adapter_not_registered",)
    return _validate_connection_against_descriptor(
        connection,
        registration.descriptor,
    )


def _validate_connection_against_descriptor(
    connection: ChannelConnectionDocument,
    descriptor: ChannelAdapterDescriptor,
) -> tuple[str, ...]:
    errors = list(_validate_config_schema(connection.config_json, descriptor))
    errors.extend(_validate_secret_requirements(connection.secret_ref, descriptor))
    return tuple(dict.fromkeys(errors))


def normalize_secret_ref(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    match = _SECRET_REF.fullmatch(normalized)
    if match is None:
        raise ChannelConnectionPayloadError(["invalid_secret_reference"])
    scheme = match.group("scheme").lower()
    locator = match.group("locator").strip().strip("/")
    if not locator:
        raise ChannelConnectionPayloadError(["invalid_secret_reference"])
    if scheme == "env" and not _ENV_NAME.fullmatch(locator):
        raise ChannelConnectionPayloadError(["invalid_environment_secret_reference"])
    if scheme not in {"env", "vault", "secret-manager"}:
        raise ChannelConnectionPayloadError(["unsupported_secret_reference_scheme"])
    return f"{scheme}://{locator}"


def _validate_secret_requirements(
    secret_ref: str,
    descriptor: ChannelAdapterDescriptor,
) -> list[str]:
    secret_field = descriptor.secret_fields[0] if descriptor.secret_fields else None
    if secret_field is not None and secret_field.required and not secret_ref:
        return ["secret_reference_required"]
    if not secret_ref:
        return []
    return _validate_provided_secret_ref(secret_ref, descriptor)


def _validate_provided_secret_ref(
    secret_ref: str,
    descriptor: ChannelAdapterDescriptor,
) -> list[str]:
    if not secret_ref:
        return []
    if not descriptor.secret_fields:
        return ["secret_reference_not_supported_by_adapter"]
    try:
        normalized = normalize_secret_ref(secret_ref)
    except ChannelConnectionPayloadError as exc:
        return list(exc.codes)
    scheme = normalized.split(":", 1)[0]
    secret_field = descriptor.secret_fields[0]
    accepted = {
        str(accepted or "").strip().lower()
        for accepted in secret_field.accepted_ref_schemes
        if str(accepted or "").strip()
    }
    if scheme not in accepted:
        return ["secret_reference_scheme_not_supported_by_adapter"]
    if scheme == "env":
        locator = normalized.split("://", 1)[1]
        allowed_variable = str(secret_field.environment_variable or "").strip()
        if not allowed_variable or locator != allowed_variable:
            return ["environment_secret_reference_not_allowed_by_adapter"]
    return []


def _validate_provided_config(
    config: dict[str, Any],
    descriptor: ChannelAdapterDescriptor,
) -> list[str]:
    # Drafts may omit required values, but values that are supplied must be
    # structurally valid. This keeps partially configured drafts possible
    # without persisting malformed URLs or unsupported fields.
    return [
        error
        for error in _validate_config_schema(config, descriptor)
        if not error.startswith("config_required_")
    ]


def _validate_config_schema(
    config: dict[str, Any],
    descriptor: ChannelAdapterDescriptor,
) -> list[str]:
    try:
        _assert_non_sensitive_config(config)
    except ChannelConnectionPayloadError as exc:
        return list(exc.codes)
    schema = descriptor.config_schema or {}
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return []
    errors: list[str] = []
    required = schema.get("required") or []
    if isinstance(required, (list, tuple)):
        for field_name in required:
            if field_name not in config or config.get(field_name) in (None, ""):
                errors.append(f"config_required_{_safe_code(field_name)}")
    if schema.get("additionalProperties") is False:
        for field_name in config:
            if field_name not in properties:
                errors.append(f"config_unknown_{_safe_code(field_name)}")
    for field_name, value in config.items():
        field_schema = properties.get(field_name)
        if not isinstance(field_schema, Mapping):
            continue
        expected = str(field_schema.get("type") or "")
        if expected and not _matches_json_type(value, expected):
            errors.append(f"config_type_{_safe_code(field_name)}")
            continue
        if "enum" in field_schema and value not in (field_schema.get("enum") or []):
            errors.append(f"config_enum_{_safe_code(field_name)}")
        if isinstance(value, str):
            minimum = int(field_schema.get("minLength") or 0)
            maximum = int(field_schema.get("maxLength") or 0)
            if minimum and len(value) < minimum:
                errors.append(f"config_min_length_{_safe_code(field_name)}")
            if maximum and len(value) > maximum:
                errors.append(f"config_max_length_{_safe_code(field_name)}")
            if field_schema.get("format") == "uri" and not _is_strict_http_uri(value):
                errors.append(f"config_format_{_safe_code(field_name)}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, float) and not math.isfinite(value):
                errors.append(f"config_number_non_finite_{_safe_code(field_name)}")
                continue
            minimum_value = field_schema.get("minimum")
            maximum_value = field_schema.get("maximum")
            if minimum_value is not None and value < minimum_value:
                errors.append(f"config_minimum_{_safe_code(field_name)}")
            if maximum_value is not None and value > maximum_value:
                errors.append(f"config_maximum_{_safe_code(field_name)}")
    return errors


def _matches_json_type(value: Any, expected: str) -> bool:
    return {
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "object": lambda item: isinstance(item, Mapping),
        "array": lambda item: isinstance(item, list),
        "null": lambda item: item is None,
    }.get(expected, lambda _item: True)(value)


def _is_strict_http_uri(value: str) -> bool:
    normalized = str(value or "")
    if not normalized or normalized != normalized.strip():
        return False
    if any(character.isspace() for character in normalized):
        return False
    if "?" in normalized or "#" in normalized:
        return False
    try:
        parsed = urlsplit(normalized)
        # Accessing port performs bracket/range validation in urllib.
        _ = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )


def _assert_non_sensitive_config(config: Mapping[str, Any], *, path: str = "config") -> None:
    for key, value in config.items():
        normalized_key = re.sub(r"[^a-z0-9]+", "", str(key or "").lower())
        if normalized_key in _SENSITIVE_PARTS or any(
            part in normalized_key for part in ("password", "secret", "token", "apikey")
        ):
            raise ChannelConnectionPayloadError(["sensitive_config_field_forbidden"])
        if isinstance(value, Mapping):
            _assert_non_sensitive_config(value, path=f"{path}.{key}")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    _assert_non_sensitive_config(item, path=f"{path}.{key}")
                elif isinstance(item, str):
                    _assert_safe_config_string(item)
        elif isinstance(value, str):
            _assert_safe_config_string(value)


def _assert_safe_config_string(value: str) -> None:
    normalized = str(value or "").strip()
    lowered = normalized.lower()
    if lowered.startswith("bearer ") or "-----begin private key-----" in lowered:
        raise ChannelConnectionPayloadError(["sensitive_config_value_forbidden"])
    if "://" not in normalized:
        return
    try:
        parsed = urlsplit(normalized)
    except ValueError as exc:
        raise ChannelConnectionPayloadError(["invalid_config_url"]) from exc
    if parsed.username is not None or parsed.password is not None:
        raise ChannelConnectionPayloadError(["config_url_userinfo_forbidden"])
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        query_key = re.sub(r"[^a-z0-9]+", "", key.lower())
        if any(
            marker in query_key
            for marker in (
                "auth",
                "credential",
                "password",
                "secret",
                "signature",
                "token",
                "apikey",
            )
        ):
            raise ChannelConnectionPayloadError(["config_url_secret_query_forbidden"])


def _normalize_adapter_config(adapter_id: str, config: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(config)
    if adapter_id == WECHAT_SDK_ADAPTER_ID and "endpoint_url" in normalized:
        endpoint = normalized.pop("endpoint_url")
        sdk_url = normalized.get("sdk_url")
        if sdk_url not in (None, "") and sdk_url != endpoint:
            raise ChannelConnectionPayloadError(["conflicting_endpoint_url"])
        normalized["sdk_url"] = endpoint
    return normalized


async def _get_document(
    conn: AsyncConnection,
    tenant_id: str,
    connection_id: str,
    *,
    for_update: bool = False,
) -> ChannelConnectionDocument:
    statement = select(ChannelConnectionRow.__table__).where(
        ChannelConnectionRow.tenant_id == tenant_id,
        ChannelConnectionRow.connection_id == connection_id,
    )
    if for_update:
        statement = statement.with_for_update()
    row = (await conn.execute(statement)).mappings().one_or_none()
    if row is None:
        raise ChannelConnectionNotFoundError(connection_id)
    return _document_from_mapping(row)


async def _cas_update(
    conn: AsyncConnection,
    before: ChannelConnectionDocument,
    *,
    expected_version: int,
    values: Mapping[str, Any],
) -> ChannelConnectionDocument:
    next_version = expected_version + 1
    statement = (
        update(ChannelConnectionRow.__table__)
        .where(
            ChannelConnectionRow.tenant_id == before.tenant_id,
            ChannelConnectionRow.connection_id == before.connection_id,
            ChannelConnectionRow.version == expected_version,
        )
        .values(
            **dict(values),
            version=next_version,
            updated_at=datetime.now(UTC),
        )
        .returning(ChannelConnectionRow.__table__)
    )
    row = (await conn.execute(statement)).mappings().one_or_none()
    if row is None:
        await _raise_current_version(conn, before, expected_version)
    assert row is not None
    return _document_from_mapping(row)


async def _raise_current_version(
    conn: AsyncConnection,
    before: ChannelConnectionDocument,
    expected_version: int,
) -> None:
    current = (
        await conn.execute(
            select(ChannelConnectionRow.version).where(
                ChannelConnectionRow.tenant_id == before.tenant_id,
                ChannelConnectionRow.connection_id == before.connection_id,
            )
        )
    ).scalar_one_or_none()
    if current is None:
        raise ChannelConnectionNotFoundError(before.connection_id)
    raise ChannelConnectionVersionConflictError(
        expected=expected_version,
        current=int(current),
    )


def _require_version(document: ChannelConnectionDocument, expected: int) -> None:
    if document.version != expected:
        raise ChannelConnectionVersionConflictError(
            expected=expected,
            current=document.version,
        )


def _document_from_row(row: ChannelConnectionRow) -> ChannelConnectionDocument:
    return ChannelConnectionDocument.model_validate(row)


def _document_from_mapping(row: Mapping[str, Any]) -> ChannelConnectionDocument:
    return ChannelConnectionDocument.model_validate(dict(row))


def _persistence_values(document: ChannelConnectionDocument) -> dict[str, Any]:
    return document.model_dump(exclude={"managed_by", "read_only", "created_at", "updated_at"})


def _document_outcome(outcome: MutationOutcome) -> ChannelConnectionMutationResult:
    return ChannelConnectionMutationResult(
        value=ChannelConnectionDocument.model_validate(outcome.response),
        status_code=outcome.status_code,
        replayed=outcome.replayed,
        mutation_id=outcome.mutation_id,
    )


def _check_outcome(outcome: MutationOutcome) -> ChannelConnectionMutationResult:
    return ChannelConnectionMutationResult(
        value=ChannelConnectionCheckResult.model_validate(outcome.response),
        status_code=outcome.status_code,
        replayed=outcome.replayed,
        mutation_id=outcome.mutation_id,
    )


def _audit_state(document: ChannelConnectionDocument) -> dict[str, Any]:
    return {
        "exists": True,
        "status": document.effective_state,
        "version": document.version,
        "enabled": document.desired_state == "enabled",
    }


def _secret_ref_fingerprint(secret_ref: str) -> str:
    if not secret_ref:
        return ""
    return hashlib.sha256(secret_ref.encode("utf-8")).hexdigest()


def connection_binding_fingerprint(connection: ChannelConnectionDocument) -> str:
    """Hash only fields that determine the adapter's live binding."""

    payload = {
        "adapter_id": connection.adapter_id,
        "config_json": connection.config_json,
        "secret_fingerprint": connection.secret_fingerprint,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _secret_status(secret_ref: str, descriptor: ChannelAdapterDescriptor) -> str:
    if secret_ref:
        return "reference_configured"
    return "missing" if descriptor.secret_fields else "not_required"


def _safe_code(value: Any) -> str:
    normalized = _SAFE_STATUS.sub("_", str(value or "").strip().lower()).strip("_.-")
    return normalized[:96]


def _normalize_connection_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not _CONNECTION_ID.fullmatch(normalized):
        raise ValueError("connection_id must be a stable identifier")
    return normalized


def _reject_legacy(connection_id: str) -> None:
    if str(connection_id or "").strip() == LEGACY_WXBOT_CONNECTION_ID:
        raise ChannelConnectionReadOnlyError("environment_managed_connection")


__all__ = [
    "LEGACY_WXBOT_CONNECTION_ID",
    "ChannelConnectionCheckResult",
    "ChannelConnectionCreateRequest",
    "ChannelConnectionDeleteResult",
    "ChannelConnectionDocument",
    "ChannelConnectionError",
    "ChannelConnectionExistsError",
    "ChannelConnectionMutationResult",
    "ChannelConnectionNotFoundError",
    "ChannelConnectionPayloadError",
    "ChannelConnectionReadOnlyError",
    "ChannelConnectionStateError",
    "ChannelConnectionStore",
    "ChannelConnectionUpdateRequest",
    "ChannelConnectionVersionConflictError",
    "connection_binding_fingerprint",
    "generated_connection_id",
    "legacy_wxbot_connection_from_settings",
    "normalize_secret_ref",
    "validate_connection_document",
]
