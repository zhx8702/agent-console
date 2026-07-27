from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from packaging.version import Version
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.admin.mutation_ledger import (
    MutationAudit,
    MutationChange,
    MutationIdentity,
    MutationOutcome,
    run_idempotent_mutation,
)
from app.infra.db import get_engine
from app.infra.runtime_schema import verify_runtime_schema
from app.plugin.base import Plugin

# ``wxbot`` remains protected from runtime uninstall for one-release
# compatibility because several built-in plugins still import its hooks and
# queue contracts.  It is an optional *adapter control surface*, not a core
# deployment dependency: no SDK connection, token, or bridge worker is implied
# by this system flag.  New channel adapters must not be added to this set.
PLUGIN_CORE_SYSTEM_NAMES = frozenset({"commands"})
PLUGIN_COMPATIBILITY_ADAPTER_NAMES = frozenset({"wxbot"})
PLUGIN_SYSTEM_NAMES = PLUGIN_CORE_SYSTEM_NAMES | PLUGIN_COMPATIBILITY_ADAPTER_NAMES

PLUGIN_STATUS_ACTIVE = "active"
PLUGIN_STATUS_DISABLED = "disabled"
PLUGIN_STATUS_FAILED = "failed"
PLUGIN_STATUS_PENDING_RESTART = "pending_restart"

PLUGIN_LIFECYCLE_IN_PROGRESS = "in_progress"
PLUGIN_LIFECYCLE_COMPLETED = "completed"

_ACTIVE_PLUGIN_STATE_CONNECTION: ContextVar[AsyncConnection | None] = ContextVar(
    "active_plugin_state_connection",
    default=None,
)


def _create_lifecycle_fence_engine() -> AsyncEngine:
    """Use a dedicated connection so the session lock cannot starve the app pool."""

    return create_async_engine(
        get_engine().url,
        poolclass=NullPool,
        pool_pre_ping=True,
        future=True,
    )


class PluginScopeVersionConflictError(RuntimeError):
    def __init__(self, *, expected: int, current: int) -> None:
        self.expected = expected
        self.current = current
        super().__init__(
            f"plugin scope version conflict: expected {expected}, current {current}"
        )


@dataclass(frozen=True)
class PluginState:
    plugin_name: str
    version: str = ""
    source: str = "builtin"
    installed: bool = True
    enabled: bool = True
    system: bool = False
    status: str = "active"
    restart_required: bool = False
    last_error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin_name": self.plugin_name,
            "name": self.plugin_name,
            "version": self.version,
            "source": self.source,
            "installed": self.installed,
            "enabled": self.enabled,
            "system": self.system,
            "status": self.status,
            "restart_required": self.restart_required,
            "last_error": self.last_error,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class PluginEvent:
    id: int
    plugin_name: str
    event_type: str
    status: str
    actor_id: str
    actor_type: str
    request_id: str
    ip_address: str
    message: str
    metadata: dict[str, Any]
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plugin_name": self.plugin_name,
            "event_type": self.event_type,
            "status": self.status,
            "actor_id": self.actor_id,
            "actor_type": self.actor_type,
            "request_id": self.request_id,
            "ip_address": self.ip_address,
            "message": self.message,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class PluginScopeState:
    tenant_id: str
    session_id: str
    plugin_name: str
    enabled: bool
    config: dict[str, Any]
    version: int
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "scope": "session" if self.session_id else "tenant",
            "plugin_name": self.plugin_name,
            "name": self.plugin_name,
            "enabled": self.enabled,
            "config": dict(self.config),
            "version": self.version,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class PluginLifecycleOperation:
    idempotency_key_hash: str
    request_fingerprint: str
    operation: str
    plugin_name: str
    status: str
    claim_token: str
    attempt_count: int
    result: dict[str, Any] | None
    before_state: dict[str, Any] | None
    after_state: dict[str, Any] | None
    policy_version: int


@dataclass(frozen=True)
class PluginLifecycleClaim:
    operation: PluginLifecycleOperation | None
    claimed: bool = False
    plugin_busy: bool = False


class PluginStateStore:
    def __init__(self) -> None:
        # In-memory test doubles commonly subclass this store while replacing
        # only ``get``/scope methods.  The marker prevents the registry from
        # selecting the SQL snapshot path unless this database-backed
        # implementation was actually initialized.
        self.database_execution_snapshot_enabled = True

    async def ensure_tables(self) -> None:
        await verify_runtime_schema(get_engine(), component="plugin state store")

    async def run_admin_mutation(
        self,
        *,
        identity: MutationIdentity,
        audit: MutationAudit,
        mutate: Callable[[], Awaitable[MutationChange]],
    ) -> MutationOutcome:
        """Commit a scope write, replay result, and semantic audit atomically."""

        async with get_engine().begin() as conn:
            token = _ACTIVE_PLUGIN_STATE_CONNECTION.set(conn)
            try:
                return await run_idempotent_mutation(
                    conn,
                    identity=identity,
                    audit=audit,
                    mutate=mutate,
                )
            finally:
                _ACTIVE_PLUGIN_STATE_CONNECTION.reset(token)

    @asynccontextmanager
    async def lifecycle_execution_fence(
        self,
        *,
        idempotency_key_hash: str,
        claim_token: str,
        lease_seconds: int = 30,
    ) -> AsyncIterator[None]:
        """Hold the global advisory lock while a claimed side effect runs."""

        engine = _create_lifecycle_fence_engine()
        params = {
            "idempotency_key_hash": idempotency_key_hash,
            "claim_token": claim_token,
            "lease_seconds": max(5, min(int(lease_seconds), 600)),
            "in_progress": PLUGIN_LIFECYCLE_IN_PROGRESS,
            "lifecycle_lock_name": "agent-console:plugin-lifecycle",
        }
        conn: AsyncConnection | None = None
        lock_acquired = False
        try:
            conn = await engine.connect()
            await conn.execute(
                text("SELECT pg_advisory_lock(hashtext(:lifecycle_lock_name))"),
                params,
            )
            lock_acquired = True
            try:
                fenced = await conn.execute(
                    text(
                        """
                        UPDATE plugin_lifecycle_operation
                        SET lease_expires_at = NOW()
                                + make_interval(secs => :lease_seconds),
                            updated_at = NOW()
                        WHERE idempotency_key_hash = :idempotency_key_hash
                          AND status = :in_progress
                          AND claim_token = :claim_token
                        RETURNING idempotency_key_hash
                        """
                    ),
                    params,
                )
                if fenced.fetchone() is None:
                    raise RuntimeError("plugin_lifecycle_claim_lost")
                await conn.commit()
                yield
            finally:
                if lock_acquired:
                    cleanup_conn = conn
                    conn = None
                    await self._settle_lifecycle_fence_cleanup(
                        cleanup_conn,
                        engine,
                        params=params,
                        release_lock=True,
                    )
        finally:
            if conn is not None:
                await self._settle_lifecycle_fence_cleanup(
                    conn,
                    engine,
                    params=params,
                    release_lock=False,
                )
            elif lock_acquired:
                # The successful-lock path disposes the engine together with
                # the connection above.
                pass
            else:
                dispose_task = asyncio.create_task(engine.dispose())
                cancelled = False
                while not dispose_task.done():
                    try:
                        await asyncio.shield(dispose_task)
                    except asyncio.CancelledError:
                        cancelled = True
                dispose_task.result()
                if cancelled:
                    raise asyncio.CancelledError()

    @staticmethod
    async def _settle_lifecycle_fence_cleanup(
        conn: AsyncConnection,
        engine: AsyncEngine,
        *,
        params: dict[str, Any],
        release_lock: bool,
    ) -> None:
        """Release/close a session lock completely before cancellation escapes."""

        async def cleanup() -> None:
            try:
                if conn.in_transaction():
                    await conn.rollback()
                if release_lock:
                    await conn.execute(
                        text(
                            "SELECT pg_advisory_unlock("
                            "hashtext(:lifecycle_lock_name))"
                        ),
                        params,
                    )
                    await conn.commit()
            finally:
                try:
                    await conn.close()
                finally:
                    await engine.dispose()

        cleanup_task = asyncio.create_task(cleanup())
        cancelled = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancelled = True
        cleanup_task.result()
        if cancelled:
            raise asyncio.CancelledError()

    async def reconcile(
        self,
        plugins: dict[str, Plugin],
        *,
        provenance: dict[str, str] | None = None,
    ) -> list[PluginState]:
        states: list[PluginState] = []
        discovery_provenance = provenance or {}
        for name, plugin in plugins.items():
            current = await self.get(name)
            metadata = self._plugin_metadata(plugin)
            discovered_source = str(
                discovery_provenance.get(name) or "manual"
            ).strip()
            metadata["discovery_provenance"] = discovered_source
            if current is None:
                trusted_builtin = discovered_source in {
                    "builtin_directory",
                    "manual",
                }
                state = PluginState(
                    plugin_name=name,
                    version=plugin.meta.version,
                    source="builtin" if trusted_builtin else discovered_source,
                    installed=trusted_builtin,
                    enabled=trusted_builtin,
                    system=name in PLUGIN_SYSTEM_NAMES,
                    status=(
                        PLUGIN_STATUS_ACTIVE
                        if trusted_builtin
                        else PLUGIN_STATUS_DISABLED
                    ),
                    metadata=metadata,
                )
                await self.create(state)
                states.append(state)
                continue
            discovered_version = plugin.meta.version
            for _attempt in range(16):
                if not (
                    current.source == "builtin"
                    and Version(discovered_version) > Version(current.version)
                ):
                    break
                await self.advance_builtin_version(
                    name,
                    expected_version=current.version,
                    discovered_version=discovered_version,
                )
                current = await self.get(name) or current
            else:
                raise RuntimeError(
                    f"builtin plugin version CAS did not converge: {name}"
                )
            acknowledge_disabled_restart = (
                current.installed
                and not current.enabled
                and current.restart_required
                and current.version == discovered_version
            )
            # Discovery reports observations; it never rewrites the desired
            # version. This prevents an old rolling-deployment replica from
            # downgrading the durable target and reopening its execution gate.
            if current.version == discovered_version:
                await self.update_discovered_metadata(name, discovered_version, metadata)
            if acknowledge_disabled_restart:
                await self.acknowledge_disabled_restart(name, discovered_version)
            refreshed = await self.get(name)
            if refreshed is not None:
                states.append(refreshed)
        return states

    async def create(self, state: PluginState) -> None:
        await self._execute(
            """
            INSERT INTO plugin_state (
                plugin_name, version, source, installed, enabled, system, status,
                restart_required, last_error, metadata_json, installed_at, updated_at
            ) VALUES (
                :plugin_name, :version, :source, :installed, :enabled, :system, :status,
                :restart_required, :last_error, CAST(:metadata_json AS JSONB), NOW(), NOW()
            )
            ON CONFLICT (plugin_name) DO NOTHING
            """,
            self._state_params(state),
        )

    async def get(self, plugin_name: str) -> PluginState | None:
        rows = await self._fetch(
            """
            SELECT plugin_name, version, source, installed, enabled, system, status,
                   restart_required, last_error, metadata_json
            FROM plugin_state
            WHERE plugin_name = :plugin_name
            """,
            {"plugin_name": plugin_name},
        )
        return self._row_to_state(rows[0]) if rows else None

    async def execution_snapshot_allowed(
        self,
        plugin_versions: dict[str, str],
        *,
        tenant_id: str = "",
        session_id: str = "",
    ) -> bool:
        """Evaluate owner/dependency state and effective scopes in one query.

        A single statement gives each execution boundary one database snapshot
        instead of issuing a global-state query plus a scope query for every
        transitive dependency.  Scope rows use session-over-tenant precedence.
        """

        requested = [
            (str(name or "").strip(), str(version or "").strip())
            for name, version in plugin_versions.items()
            if str(name or "").strip()
        ]
        if not requested:
            return False
        rows = await self._fetch(
            """
            WITH requested(plugin_name, expected_version) AS (
                SELECT *
                FROM unnest(
                    CAST(:plugin_names AS TEXT[]),
                    CAST(:plugin_versions AS TEXT[])
                )
            )
            SELECT
                COUNT(*) AS requested_count,
                COUNT(state.plugin_name) AS present_count,
                COALESCE(
                    BOOL_AND(
                        state.plugin_name IS NOT NULL
                        AND state.installed = TRUE
                        AND state.enabled = TRUE
                        AND state.status = 'active'
                        AND state.restart_required = FALSE
                        AND state.version = requested.expected_version
                        AND COALESCE(scope.enabled, TRUE)
                    ),
                    FALSE
                ) AS allowed
            FROM requested
            LEFT JOIN plugin_state state
              ON state.plugin_name = requested.plugin_name
            LEFT JOIN LATERAL (
                SELECT scoped.enabled
                FROM plugin_scope_state scoped
                WHERE CAST(:tenant_id AS TEXT) != ''
                  AND scoped.tenant_id = CAST(:tenant_id AS TEXT)
                  AND scoped.plugin_name = requested.plugin_name
                  AND (
                      scoped.session_id = ''
                      OR scoped.session_id = CAST(:session_id AS TEXT)
                  )
                ORDER BY CASE
                    WHEN scoped.session_id = CAST(:session_id AS TEXT) THEN 0
                    ELSE 1
                END
                LIMIT 1
            ) scope ON TRUE
            """,
            {
                "plugin_names": [name for name, _version in requested],
                "plugin_versions": [version for _name, version in requested],
                "tenant_id": str(tenant_id or "").strip(),
                "session_id": str(session_id or "").strip(),
            },
        )
        if not rows:
            return False
        row = rows[0]
        return bool(
            int(row.get("requested_count") or 0) == len(requested)
            and int(row.get("present_count") or 0) == len(requested)
            and row.get("allowed") is True
        )

    async def list_installed(self) -> list[PluginState]:
        rows = await self._fetch(
            """
            SELECT plugin_name, version, source, installed, enabled, system, status,
                   restart_required, last_error, metadata_json
            FROM plugin_state
            WHERE installed = TRUE
            ORDER BY plugin_name
            """
        )
        return [self._row_to_state(row) for row in rows]

    async def list_states(self) -> list[PluginState]:
        rows = await self._fetch(
            """
            SELECT plugin_name, version, source, installed, enabled, system, status,
                   restart_required, last_error, metadata_json
            FROM plugin_state
            ORDER BY plugin_name
            """
        )
        return [self._row_to_state(row) for row in rows]

    async def has_pending_restart(self, *, exclude_plugin_name: str = "") -> bool:
        rows = await self._fetch(
            """
            SELECT plugin_name
            FROM plugin_state
            WHERE restart_required = TRUE
              AND (:exclude_plugin_name = '' OR plugin_name != :exclude_plugin_name)
            LIMIT 1
            """,
            {"exclude_plugin_name": exclude_plugin_name},
        )
        return bool(rows)

    async def upsert_marketplace_install(
        self,
        *,
        plugin_name: str,
        version: str,
        source: str,
        system: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> PluginState | None:
        await self._execute(
            """
            INSERT INTO plugin_state (
                plugin_name, version, source, installed, enabled, system, status,
                restart_required, last_error, metadata_json, installed_at, updated_at
            ) VALUES (
                :plugin_name, :version, :source, TRUE, FALSE, :system, :status,
                TRUE, '', CAST(:metadata_json AS JSONB), NOW(), NOW()
            )
            ON CONFLICT (plugin_name) DO UPDATE
            SET version = EXCLUDED.version,
                source = EXCLUDED.source,
                installed = TRUE,
                enabled = FALSE,
                system = EXCLUDED.system,
                status = :status,
                restart_required = TRUE,
                last_error = '',
                metadata_json = EXCLUDED.metadata_json,
                updated_at = NOW()
            """,
            {
                "plugin_name": plugin_name,
                "version": version,
                "source": source,
                "system": system,
                "status": PLUGIN_STATUS_PENDING_RESTART,
                "metadata_json": json.dumps(metadata or {}),
            },
        )
        return await self.get(plugin_name)

    async def mark_upgraded(
        self,
        *,
        plugin_name: str,
        version: str,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> PluginState | None:
        await self._execute(
            """
            UPDATE plugin_state
            SET version = :version,
                source = :source,
                installed = TRUE,
                status = :status,
                restart_required = TRUE,
                last_error = '',
                metadata_json = CAST(:metadata_json AS JSONB),
                updated_at = NOW()
            WHERE plugin_name = :plugin_name
            """,
            {
                "plugin_name": plugin_name,
                "version": version,
                "source": source,
                "status": PLUGIN_STATUS_PENDING_RESTART,
                "metadata_json": json.dumps(metadata or {}),
            },
        )
        return await self.get(plugin_name)

    async def mark_uninstalled(self, plugin_name: str) -> PluginState | None:
        await self._execute(
            """
            UPDATE plugin_state
            SET installed = FALSE,
                enabled = FALSE,
                status = :status,
                restart_required = TRUE,
                last_error = '',
                updated_at = NOW()
            WHERE plugin_name = :plugin_name
            """,
            {"plugin_name": plugin_name, "status": PLUGIN_STATUS_PENDING_RESTART},
        )
        return await self.get(plugin_name)

    async def update_discovered_metadata(
        self,
        plugin_name: str,
        version: str,
        metadata: dict[str, Any],
    ) -> None:
        await self._execute(
            """
            UPDATE plugin_state
            SET metadata_json = COALESCE(metadata_json, '{}'::JSONB)
                    || CAST(:metadata_json AS JSONB),
                updated_at = NOW()
            WHERE plugin_name = :plugin_name
            """,
            {
                "plugin_name": plugin_name,
                "metadata_json": json.dumps(metadata),
            },
        )

    async def advance_builtin_version(
        self,
        plugin_name: str,
        *,
        expected_version: str,
        discovered_version: str,
    ) -> bool:
        """Monotonically advance an image-owned plugin with a CAS fence."""

        rows = await self._fetch(
            """
            UPDATE plugin_state
            SET version = :discovered_version,
                status = :status,
                restart_required = TRUE,
                last_error = '',
                updated_at = NOW()
            WHERE plugin_name = :plugin_name
              AND source = 'builtin'
              AND version = :expected_version
            RETURNING plugin_name
            """,
            {
                "plugin_name": plugin_name,
                "expected_version": expected_version,
                "discovered_version": discovered_version,
                "status": PLUGIN_STATUS_PENDING_RESTART,
            },
        )
        return bool(rows)

    async def acknowledge_disabled_restart(
        self,
        plugin_name: str,
        discovered_version: str,
    ) -> None:
        """Acknowledge a completed restart for an installed disabled plugin.

        Disabled plugins are deliberately excluded from runtime initialization,
        so they cannot rely on ``mark_initialized`` to clear a restart requested
        by install, upgrade, or disable.  Reconciliation may clear that marker
        only when this process discovered the exact persisted artifact version.
        Enabled plugins remain pending until initialization succeeds.
        """

        await self._execute(
            """
            UPDATE plugin_state
            SET status = :status,
                restart_required = FALSE,
                last_error = '',
                updated_at = NOW()
            WHERE plugin_name = :plugin_name
              AND installed = TRUE
              AND enabled = FALSE
              AND restart_required = TRUE
              AND version = :discovered_version
            """,
            {
                "plugin_name": plugin_name,
                "discovered_version": discovered_version,
                "status": PLUGIN_STATUS_DISABLED,
            },
        )

    async def acknowledge_uninstalled_restarts(self) -> None:
        """Clear uninstall fences after a fresh process skipped their code."""

        await self._execute(
            """
            UPDATE plugin_state
            SET status = :status,
                restart_required = FALSE,
                last_error = '',
                updated_at = NOW()
            WHERE installed = FALSE
              AND enabled = FALSE
              AND restart_required = TRUE
            """,
            {"status": PLUGIN_STATUS_DISABLED},
        )

    async def mark_initialized(self, plugin_name: str, expected_version: str) -> bool:
        rows = await self._fetch(
            """
            UPDATE plugin_state
            SET status = :status, last_error = '', restart_required = FALSE, updated_at = NOW()
            WHERE plugin_name = :plugin_name
              AND installed = TRUE
              AND enabled = TRUE
              AND version = :expected_version
            RETURNING plugin_name
            """,
            {
                "plugin_name": plugin_name,
                "expected_version": expected_version,
                "status": PLUGIN_STATUS_ACTIVE,
            },
        )
        return bool(rows)

    async def mark_failed(
        self,
        plugin_name: str,
        error: str,
        expected_version: str,
    ) -> bool:
        rows = await self._fetch(
            """
            UPDATE plugin_state
            SET status = :status, last_error = :last_error, updated_at = NOW()
            WHERE plugin_name = :plugin_name
              AND installed = TRUE
              AND enabled = TRUE
              AND version = :expected_version
            RETURNING plugin_name
            """,
            {
                "plugin_name": plugin_name,
                "expected_version": expected_version,
                "status": PLUGIN_STATUS_FAILED,
                "last_error": error[:4000],
            },
        )
        return bool(rows)

    async def set_enabled(
        self,
        plugin_name: str,
        enabled: bool,
        *,
        restart_required: bool = False,
    ) -> PluginState | None:
        status = PLUGIN_STATUS_PENDING_RESTART if restart_required else (
            PLUGIN_STATUS_ACTIVE if enabled else PLUGIN_STATUS_DISABLED
        )
        await self._execute(
            """
            UPDATE plugin_state
            SET enabled = :enabled,
                status = :status,
                restart_required = :restart_required,
                last_error = '',
                updated_at = NOW()
            WHERE plugin_name = :plugin_name
            """,
            {
                "plugin_name": plugin_name,
                "enabled": enabled,
                "status": status,
                "restart_required": restart_required,
            },
        )
        return await self.get(plugin_name)

    async def append_event(
        self,
        plugin_name: str,
        event_type: str,
        *,
        status: str = "ok",
        actor_id: str = "",
        request_id: str = "",
        ip_address: str = "",
        message: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._execute(
            """
            INSERT INTO plugin_events (
                plugin_name, event_type, status, actor_id, actor_type, request_id,
                ip_address, message, metadata_json, created_at
            ) VALUES (
                :plugin_name, :event_type, :status, :actor_id, 'admin', :request_id,
                :ip_address, :message, CAST(:metadata_json AS JSONB), NOW()
            )
            """,
            {
                "plugin_name": plugin_name,
                "event_type": event_type,
                "status": status,
                "actor_id": actor_id,
                "request_id": request_id,
                "ip_address": ip_address,
                "message": message,
                "metadata_json": json.dumps(metadata or {}),
            },
        )

    async def list_events(
        self,
        *,
        plugin_name: str = "",
        event_type: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[PluginEvent]:
        rows = await self._fetch(
            """
            SELECT id, plugin_name, event_type, status, actor_id, actor_type, request_id,
                   ip_address, message, metadata_json, created_at
            FROM plugin_events
            WHERE (:plugin_name = '' OR plugin_name = :plugin_name)
              AND (:event_type = '' OR event_type = :event_type)
            ORDER BY created_at DESC, id DESC
            LIMIT :limit OFFSET :offset
            """,
            {
                "plugin_name": plugin_name,
                "event_type": event_type,
                "limit": max(1, min(limit, 500)),
                "offset": max(0, offset),
            },
        )
        return [self._row_to_event(row) for row in rows]

    async def list_scope_states(
        self,
        *,
        tenant_id: str,
        session_id: str | None = None,
        plugin_name: str = "",
    ) -> list[PluginScopeState]:
        rows = await self._fetch(
            """
            SELECT tenant_id, session_id, plugin_name, enabled, config_json, version, updated_at
            FROM plugin_scope_state
            WHERE tenant_id = :tenant_id
              AND (CAST(:session_id AS TEXT) IS NULL OR session_id = CAST(:session_id AS TEXT))
              AND (CAST(:plugin_name AS TEXT) = '' OR plugin_name = CAST(:plugin_name AS TEXT))
            ORDER BY session_id, plugin_name
            """,
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "plugin_name": plugin_name,
            },
        )
        return [self._row_to_scope_state(row) for row in rows]

    async def resolve_effective_scope(
        self,
        tenant_id: str,
        session_id: str,
        plugin_name: str,
    ) -> PluginScopeState | None:
        """Resolve the session override, falling back to the tenant scope."""

        rows = await self._fetch(
            """
            SELECT tenant_id, session_id, plugin_name, enabled, config_json, version, updated_at
            FROM plugin_scope_state
            WHERE tenant_id = :tenant_id
              AND plugin_name = :plugin_name
              AND (session_id = '' OR session_id = :session_id)
            ORDER BY CASE WHEN session_id = :session_id THEN 0 ELSE 1 END
            LIMIT 1
            """,
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "plugin_name": plugin_name,
            },
        )
        return self._row_to_scope_state(rows[0]) if rows else None

    async def set_scope_enabled(
        self,
        *,
        tenant_id: str,
        session_id: str,
        plugin_name: str,
        enabled: bool,
        expected_version: int,
        config: dict[str, Any] | None = None,
    ) -> PluginScopeState | None:
        expected = max(0, int(expected_version))
        before_rows = await self.list_scope_states(
            tenant_id=tenant_id,
            session_id=session_id,
            plugin_name=plugin_name,
        )
        current = before_rows[0].version if before_rows else 0
        if current != expected:
            raise PluginScopeVersionConflictError(expected=expected, current=current)
        rows = await self._fetch(
            """
            INSERT INTO plugin_scope_state (
                tenant_id, session_id, plugin_name, enabled, config_json, version, updated_at
            ) VALUES (
                :tenant_id, :session_id, :plugin_name, :enabled,
                CAST(:config_json AS JSONB), 1, NOW()
            )
            ON CONFLICT (tenant_id, session_id, plugin_name) DO UPDATE
            SET enabled = EXCLUDED.enabled,
                config_json = EXCLUDED.config_json,
                version = plugin_scope_state.version + 1,
                updated_at = NOW()
            WHERE plugin_scope_state.version = :expected_version
            RETURNING tenant_id, session_id, plugin_name, enabled, config_json,
                      version, updated_at
            """,
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "plugin_name": plugin_name,
                "enabled": enabled,
                "config_json": json.dumps(config or {}),
                "expected_version": expected,
            },
        )
        if rows:
            return self._row_to_scope_state(rows[0])
        current_rows = await self.list_scope_states(
            tenant_id=tenant_id,
            session_id=session_id,
            plugin_name=plugin_name,
        )
        current = current_rows[0].version if current_rows else 0
        raise PluginScopeVersionConflictError(
            expected=expected,
            current=current,
        )

    async def claim_lifecycle_operation(
        self,
        *,
        idempotency_key_hash: str,
        request_fingerprint: str,
        operation: str,
        plugin_name: str,
        claim_token: str,
        lease_seconds: int = 30,
    ) -> PluginLifecycleClaim:
        """Claim a durable lifecycle intent under one global control-plane lock."""

        engine = get_engine()
        params = {
            "idempotency_key_hash": idempotency_key_hash,
            "request_fingerprint": request_fingerprint,
            "operation": operation,
            "plugin_name": plugin_name,
            "claim_token": claim_token,
            "lease_seconds": max(5, min(int(lease_seconds), 600)),
            "lifecycle_lock_name": "agent-console:plugin-lifecycle",
        }
        async with engine.begin() as conn:
            # Plugin dependencies turn apparently independent target writes
            # into one graph mutation. Serializing the low-volume lifecycle
            # control plane closes enable/disable and upgrade/dependency races.
            lock_result = await conn.execute(
                text(
                    "SELECT pg_try_advisory_xact_lock("
                    "hashtext(:lifecycle_lock_name))"
                ),
                {"lifecycle_lock_name": params["lifecycle_lock_name"]},
            )
            if not bool(lock_result.scalar_one()):
                # Do not let concurrent admin requests occupy every shared DB
                # connection while a long lifecycle action holds the session
                # fence. Same-key callers may still observe and wait/replay an
                # already committed durable record without taking the lock.
                observed_result = await conn.execute(
                    text(
                        """
                        SELECT idempotency_key_hash, request_fingerprint, operation,
                               plugin_name, status, claim_token, attempt_count,
                               result_json, before_state_json, after_state_json,
                               policy_version
                        FROM plugin_lifecycle_operation
                        WHERE idempotency_key_hash = :idempotency_key_hash
                        """
                    ),
                    params,
                )
                observed_row = observed_result.fetchone()
                if observed_row is not None:
                    return PluginLifecycleClaim(
                        operation=self._row_to_lifecycle_operation(
                            dict(observed_row._mapping)
                        )
                    )
                return PluginLifecycleClaim(operation=None, plugin_busy=True)
            existing_result = await conn.execute(
                text(
                    """
                    SELECT idempotency_key_hash, request_fingerprint, operation,
                           plugin_name, status, claim_token, attempt_count,
                           result_json, before_state_json, after_state_json,
                           policy_version
                    FROM plugin_lifecycle_operation
                    WHERE idempotency_key_hash = :idempotency_key_hash
                    FOR UPDATE
                    """
                ),
                params,
            )
            existing_row = existing_result.fetchone()
            if existing_row is not None:
                existing = self._row_to_lifecycle_operation(dict(existing_row._mapping))
                if (
                    existing.request_fingerprint != request_fingerprint
                    or existing.operation != operation
                    or existing.plugin_name != plugin_name
                ):
                    return PluginLifecycleClaim(operation=existing)
                if existing.status == PLUGIN_LIFECYCLE_COMPLETED:
                    return PluginLifecycleClaim(operation=existing)
                if existing.claim_token == claim_token:
                    return PluginLifecycleClaim(operation=existing, claimed=True)
                reclaimed_result = await conn.execute(
                    text(
                        """
                        UPDATE plugin_lifecycle_operation
                        SET claim_token = :claim_token,
                            lease_expires_at = NOW() + make_interval(secs => :lease_seconds),
                            attempt_count = attempt_count + 1,
                            updated_at = NOW()
                        WHERE idempotency_key_hash = :idempotency_key_hash
                          AND status = :in_progress
                          AND (
                              claim_token = ''
                              OR lease_expires_at IS NULL
                              OR lease_expires_at <= NOW()
                          )
                        RETURNING idempotency_key_hash, request_fingerprint, operation,
                                  plugin_name, status, claim_token, attempt_count,
                                  result_json, before_state_json, after_state_json,
                                  policy_version
                        """
                    ),
                    {**params, "in_progress": PLUGIN_LIFECYCLE_IN_PROGRESS},
                )
                reclaimed_row = reclaimed_result.fetchone()
                if reclaimed_row is not None:
                    return PluginLifecycleClaim(
                        operation=self._row_to_lifecycle_operation(dict(reclaimed_row._mapping)),
                        claimed=True,
                    )
                return PluginLifecycleClaim(operation=existing)

            # A crashed request with a different idempotency key must not
            # permanently deadlock this plugin. Same-key callers get the
            # reclaim path above; a later distinct intent terminalizes only an
            # expired/explicitly released predecessor and receives a fresh
            # claim. Replaying the predecessor returns this deterministic 409.
            await conn.execute(
                text(
                    """
                    UPDATE plugin_lifecycle_operation
                    SET status = :completed,
                        result_json = CAST(:expired_result_json AS JSONB),
                        after_state_json = COALESCE(
                            after_state_json,
                            before_state_json,
                            '{}'::JSONB
                        ),
                        policy_version = GREATEST(policy_version + 1, 1),
                        claim_token = '',
                        lease_expires_at = NULL,
                        last_error_code = :expired_error_code,
                        completed_at = NOW(),
                        updated_at = NOW()
                    WHERE status = :in_progress
                      AND (
                          claim_token = ''
                          OR lease_expires_at IS NULL
                          OR lease_expires_at <= NOW()
                      )
                    """
                ),
                {
                    **params,
                    "in_progress": PLUGIN_LIFECYCLE_IN_PROGRESS,
                    "completed": PLUGIN_LIFECYCLE_COMPLETED,
                    "expired_error_code": "plugin_lifecycle_lease_expired",
                    "expired_result_json": json.dumps(
                        {
                            "kind": "http_error",
                            "status_code": 409,
                            "detail": "plugin_lifecycle_lease_expired",
                            "headers": {},
                        }
                    ),
                },
            )

            busy_result = await conn.execute(
                text(
                    """
                    SELECT idempotency_key_hash, request_fingerprint, operation,
                           plugin_name, status, claim_token, attempt_count,
                           result_json, before_state_json, after_state_json,
                           policy_version
                    FROM plugin_lifecycle_operation
                    WHERE status = :in_progress
                    ORDER BY created_at ASC
                    LIMIT 1
                    FOR UPDATE
                    """
                ),
                {**params, "in_progress": PLUGIN_LIFECYCLE_IN_PROGRESS},
            )
            busy_row = busy_result.fetchone()
            if busy_row is not None:
                return PluginLifecycleClaim(
                    operation=self._row_to_lifecycle_operation(dict(busy_row._mapping)),
                    plugin_busy=True,
                )

            inserted_result = await conn.execute(
                text(
                    """
                    INSERT INTO plugin_lifecycle_operation (
                        idempotency_key_hash, request_fingerprint, operation,
                        plugin_name, status, claim_token, lease_expires_at,
                        attempt_count, policy_version, created_at, updated_at
                    ) VALUES (
                        :idempotency_key_hash, :request_fingerprint, :operation,
                        :plugin_name, :in_progress, :claim_token,
                        NOW() + make_interval(secs => :lease_seconds),
                        1, 0, NOW(), NOW()
                    )
                    RETURNING idempotency_key_hash, request_fingerprint, operation,
                              plugin_name, status, claim_token, attempt_count,
                              result_json, before_state_json, after_state_json,
                              policy_version
                    """
                ),
                {**params, "in_progress": PLUGIN_LIFECYCLE_IN_PROGRESS},
            )
            inserted_row = inserted_result.fetchone()
            if inserted_row is None:  # pragma: no cover - INSERT RETURNING is deterministic
                raise RuntimeError("plugin_lifecycle_claim_not_created")
            return PluginLifecycleClaim(
                operation=self._row_to_lifecycle_operation(dict(inserted_row._mapping)),
                claimed=True,
            )

    async def record_lifecycle_before_state(
        self,
        *,
        idempotency_key_hash: str,
        claim_token: str,
        before_state: dict[str, Any],
    ) -> PluginLifecycleOperation:
        rows = await self._fetch(
            """
            UPDATE plugin_lifecycle_operation
            SET before_state_json = COALESCE(
                    before_state_json,
                    CAST(:before_state_json AS JSONB)
                ),
                updated_at = NOW()
            WHERE idempotency_key_hash = :idempotency_key_hash
              AND status = :in_progress
              AND claim_token = :claim_token
            RETURNING idempotency_key_hash, request_fingerprint, operation,
                      plugin_name, status, claim_token, attempt_count,
                      result_json, before_state_json, after_state_json,
                      policy_version
            """,
            {
                "idempotency_key_hash": idempotency_key_hash,
                "claim_token": claim_token,
                "in_progress": PLUGIN_LIFECYCLE_IN_PROGRESS,
                "before_state_json": json.dumps(before_state),
            },
        )
        if not rows:
            raise RuntimeError("plugin_lifecycle_claim_lost")
        return self._row_to_lifecycle_operation(rows[0])

    async def complete_lifecycle_operation(
        self,
        *,
        idempotency_key_hash: str,
        claim_token: str,
        result: dict[str, Any],
        after_state: dict[str, Any],
    ) -> PluginLifecycleOperation:
        rows = await self._fetch(
            """
            UPDATE plugin_lifecycle_operation
            SET status = :completed,
                result_json = CAST(:result_json AS JSONB),
                after_state_json = CAST(:after_state_json AS JSONB),
                policy_version = GREATEST(policy_version + 1, 1),
                claim_token = '',
                lease_expires_at = NULL,
                last_error_code = '',
                completed_at = NOW(),
                updated_at = NOW()
            WHERE idempotency_key_hash = :idempotency_key_hash
              AND status = :in_progress
              AND claim_token = :claim_token
            RETURNING idempotency_key_hash, request_fingerprint, operation,
                      plugin_name, status, claim_token, attempt_count,
                      result_json, before_state_json, after_state_json,
                      policy_version
            """,
            {
                "idempotency_key_hash": idempotency_key_hash,
                "claim_token": claim_token,
                "in_progress": PLUGIN_LIFECYCLE_IN_PROGRESS,
                "completed": PLUGIN_LIFECYCLE_COMPLETED,
                "result_json": json.dumps(result),
                "after_state_json": json.dumps(after_state),
            },
        )
        if rows:
            return self._row_to_lifecycle_operation(rows[0])

        # If the connection failed after COMMIT, re-reading the durable result
        # turns the ambiguous outcome into a safe replay instead of redoing it.
        existing = await self.get_lifecycle_operation(idempotency_key_hash)
        if existing is not None and existing.status == PLUGIN_LIFECYCLE_COMPLETED:
            return existing
        raise RuntimeError("plugin_lifecycle_claim_lost")

    async def renew_lifecycle_claim(
        self,
        *,
        idempotency_key_hash: str,
        claim_token: str,
        lease_seconds: int = 30,
    ) -> bool:
        """Extend a live claim without changing its attempt or result state."""

        rows = await self._fetch(
            """
            UPDATE plugin_lifecycle_operation
            SET lease_expires_at = NOW() + make_interval(secs => :lease_seconds),
                updated_at = NOW()
            WHERE idempotency_key_hash = :idempotency_key_hash
              AND status = :in_progress
              AND claim_token = :claim_token
            RETURNING idempotency_key_hash
            """,
            {
                "idempotency_key_hash": idempotency_key_hash,
                "claim_token": claim_token,
                "in_progress": PLUGIN_LIFECYCLE_IN_PROGRESS,
                "lease_seconds": max(5, min(int(lease_seconds), 600)),
            },
        )
        return bool(rows)

    async def get_lifecycle_operation(
        self,
        idempotency_key_hash: str,
    ) -> PluginLifecycleOperation | None:
        rows = await self._fetch(
            """
            SELECT idempotency_key_hash, request_fingerprint, operation,
                   plugin_name, status, claim_token, attempt_count,
                   result_json, before_state_json, after_state_json,
                   policy_version
            FROM plugin_lifecycle_operation
            WHERE idempotency_key_hash = :idempotency_key_hash
            """,
            {"idempotency_key_hash": idempotency_key_hash},
        )
        return self._row_to_lifecycle_operation(rows[0]) if rows else None

    async def release_lifecycle_claim(
        self,
        *,
        idempotency_key_hash: str,
        claim_token: str,
        error_code: str,
    ) -> None:
        await self._execute(
            """
            UPDATE plugin_lifecycle_operation
            SET claim_token = '',
                lease_expires_at = NOW(),
                last_error_code = :error_code,
                updated_at = NOW()
            WHERE idempotency_key_hash = :idempotency_key_hash
              AND status = :in_progress
              AND claim_token = :claim_token
            """,
            {
                "idempotency_key_hash": idempotency_key_hash,
                "claim_token": claim_token,
                "in_progress": PLUGIN_LIFECYCLE_IN_PROGRESS,
                "error_code": str(error_code or "operation_failed")[:128],
            },
        )

    @staticmethod
    def _plugin_metadata(plugin: Plugin) -> dict[str, Any]:
        return {
            "description": plugin.meta.description,
            "dependencies": list(plugin.meta.dependencies),
            "permissions": list(plugin.get_permissions()),
            "admin_ui": plugin.get_admin_ui(),
        }

    @staticmethod
    def _state_params(state: PluginState) -> dict[str, Any]:
        return {
            "plugin_name": state.plugin_name,
            "version": state.version,
            "source": state.source,
            "installed": state.installed,
            "enabled": state.enabled,
            "system": state.system,
            "status": state.status,
            "restart_required": state.restart_required,
            "last_error": state.last_error,
            "metadata_json": json.dumps(state.metadata),
        }

    @staticmethod
    def _row_to_state(row: dict[str, Any]) -> PluginState:
        metadata = row.get("metadata_json") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata or "{}")
        return PluginState(
            plugin_name=str(row.get("plugin_name") or ""),
            version=str(row.get("version") or ""),
            source=str(row.get("source") or "builtin"),
            installed=bool(row.get("installed")),
            enabled=bool(row.get("enabled")),
            system=bool(row.get("system")),
            status=str(row.get("status") or "unknown"),
            restart_required=bool(row.get("restart_required")),
            last_error=str(row.get("last_error") or ""),
            metadata=dict(metadata),
        )

    @staticmethod
    def _row_to_event(row: dict[str, Any]) -> PluginEvent:
        metadata = row.get("metadata_json") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata or "{}")
        return PluginEvent(
            id=int(row.get("id") or 0),
            plugin_name=str(row.get("plugin_name") or ""),
            event_type=str(row.get("event_type") or ""),
            status=str(row.get("status") or ""),
            actor_id=str(row.get("actor_id") or ""),
            actor_type=str(row.get("actor_type") or ""),
            request_id=str(row.get("request_id") or ""),
            ip_address=str(row.get("ip_address") or ""),
            message=str(row.get("message") or ""),
            metadata=dict(metadata),
            created_at=str(row.get("created_at") or ""),
        )

    @staticmethod
    def _row_to_scope_state(row: dict[str, Any]) -> PluginScopeState:
        config = row.get("config_json") or {}
        if isinstance(config, str):
            config = json.loads(config or "{}")
        return PluginScopeState(
            tenant_id=str(row.get("tenant_id") or ""),
            session_id=str(row.get("session_id") or ""),
            plugin_name=str(row.get("plugin_name") or ""),
            enabled=bool(row.get("enabled")),
            config=dict(config),
            version=max(0, int(row.get("version") or 0)),
            updated_at=str(row.get("updated_at") or ""),
        )

    @staticmethod
    def _row_to_lifecycle_operation(row: dict[str, Any]) -> PluginLifecycleOperation:
        def _mapping(value: Any) -> dict[str, Any] | None:
            if value is None:
                return None
            if isinstance(value, str):
                decoded = json.loads(value or "{}")
                return dict(decoded) if isinstance(decoded, dict) else {}
            return dict(value) if isinstance(value, dict) else {}

        return PluginLifecycleOperation(
            idempotency_key_hash=str(row.get("idempotency_key_hash") or ""),
            request_fingerprint=str(row.get("request_fingerprint") or ""),
            operation=str(row.get("operation") or ""),
            plugin_name=str(row.get("plugin_name") or ""),
            status=str(row.get("status") or ""),
            claim_token=str(row.get("claim_token") or ""),
            attempt_count=max(0, int(row.get("attempt_count") or 0)),
            result=_mapping(row.get("result_json")),
            before_state=_mapping(row.get("before_state_json")),
            after_state=_mapping(row.get("after_state_json")),
            policy_version=max(0, int(row.get("policy_version") or 0)),
        )

    @staticmethod
    async def _fetch(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        active = _ACTIVE_PLUGIN_STATE_CONNECTION.get()
        if active is not None:
            result = await active.execute(text(sql), params or {})
            if result.returns_rows:
                return [dict(row._mapping) for row in result.fetchall()]
            return []
        engine = get_engine()
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params or {})
            if result.returns_rows:
                return [dict(row._mapping) for row in result.fetchall()]
            return []

    @classmethod
    async def _execute(cls, sql: str, params: dict[str, Any] | None = None) -> None:
        await cls._fetch(sql, params)
