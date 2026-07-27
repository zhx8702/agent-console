from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
import uuid
import zipfile
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, TypeVar

from fastapi import HTTPException, Request
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from app.common.logging import get_logger
from app.plugin.artifacts import compute_plugin_tree_digest
from app.plugin.base import PluginContext
from app.plugin.config_schema import (
    PluginConfigSchemaError,
    PluginConfigValidationError,
    validate_plugin_config,
)
from app.plugin.dependencies import PluginDependencyError, parse_plugin_dependency
from app.plugin.marketplace import (
    MarketplaceItem,
    MarketplaceManifestError,
    PluginDependency,
    load_marketplace_manifest,
    normalize_specifier,
    permission_delta,
    validate_plugin_name,
)
from app.plugin.registry import PluginRegistry
from app.plugin.state import (
    PLUGIN_LIFECYCLE_COMPLETED,
    PLUGIN_SYSTEM_NAMES,
    PluginLifecycleOperation,
    PluginState,
    PluginStateStore,
)

logger = get_logger(__name__)

_T = TypeVar("_T")

_PLUGIN_LIFECYCLE_OPERATIONS = frozenset(
    {"install", "enable", "disable", "upgrade", "uninstall"}
)
_PLUGIN_LIFECYCLE_LEASE_SECONDS = 30
_PLUGIN_LIFECYCLE_WAIT_SECONDS = 15.0

# Local packages execute in the API process after restart, so their extraction
# boundary must be deliberately small and deterministic. These limits apply to
# both central-directory metadata and the bytes actually written to staging.
_LOCAL_ARCHIVE_MAX_MEMBERS = 512
_LOCAL_ARCHIVE_MAX_MEMBER_BYTES = 16 * 1024 * 1024
_LOCAL_ARCHIVE_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_LOCAL_ARCHIVE_MAX_FILE_BYTES = _LOCAL_ARCHIVE_MAX_TOTAL_BYTES + 1024 * 1024
_LOCAL_ARCHIVE_MAX_COMPRESSION_RATIO = 200.0
_LOCAL_ARCHIVE_ALLOWED_COMPRESSION = frozenset(
    {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
)
_LOCAL_ARCHIVE_DESCRIPTOR_NAME = "plugin-package.json"
_LOCAL_ARCHIVE_DESCRIPTOR_SCHEMA_VERSION = 2
_LOCAL_ARCHIVE_DESCRIPTOR_FIELDS = frozenset(
    {
        "schema_version",
        "name",
        "version",
        "compatibility",
        "permissions",
        "dependencies",
        "capability_digest",
    }
)
_LOCAL_ARCHIVE_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)

_ACTIVE_PLUGIN_LIFECYCLE_CLAIM: ContextVar[tuple[str, str] | None] = ContextVar(
    "active_plugin_lifecycle_claim",
    default=None,
)
_ACTIVE_PLUGIN_MARKETPLACE_ITEM: ContextVar[MarketplaceItem | None] = ContextVar(
    "active_plugin_marketplace_item",
    default=None,
)


def _remove_local_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


async def _settle_under_cancellation(
    operation: Awaitable[_T],
    *,
    label: str,
) -> _T:
    """Finish a compensation/commit before propagating repeated cancellation."""

    task = asyncio.ensure_future(operation)
    cancellation_requested = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation_requested = True
    if cancellation_requested:
        try:
            task.result()
        except BaseException as exc:
            logger.error(
                "plugin.lifecycle_settlement_failed_during_cancellation",
                label=label,
                error=type(exc).__name__,
            )
        raise asyncio.CancelledError()
    return task.result()


@dataclass(frozen=True, slots=True)
class PluginLifecycleExecutionResult:
    response: dict[str, Any]
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    policy_version: int
    idempotent_replayed: bool = False


@dataclass(slots=True)
class _ArtifactInstallTransaction:
    """Filesystem half of a local-package install, pending durable commit."""

    metadata: dict[str, Any]
    target_dir: Path
    staging_dir: Path
    backup_dir: Path
    activated: bool = False

    def activate(self) -> None:
        if self.activated:
            return
        try:
            if self.target_dir.exists() or self.target_dir.is_symlink():
                self.target_dir.rename(self.backup_dir)
            self.staging_dir.rename(self.target_dir)
            self.activated = True
        except BaseException:
            if self.backup_dir.exists() and not self.target_dir.exists():
                self.backup_dir.rename(self.target_dir)
            raise

    def commit(self) -> None:
        if not self.activated:
            raise RuntimeError("plugin artifact transaction is not active")
        _remove_local_path(self.backup_dir)
        _remove_local_path(self.staging_dir)

    def rollback(self) -> None:
        if self.activated:
            _remove_local_path(self.target_dir)
            if self.backup_dir.exists() or self.backup_dir.is_symlink():
                self.backup_dir.rename(self.target_dir)
        _remove_local_path(self.staging_dir)


class PluginManager:
    def __init__(
        self,
        registry: PluginRegistry,
        state_store: PluginStateStore,
        ctx: PluginContext,
    ) -> None:
        self.registry = registry
        self.state_store = state_store
        self.ctx = ctx

    async def installed(self) -> dict[str, Any]:
        states = await self.state_store.list_installed()
        return {"plugins": [self._installed_payload(state) for state in states]}

    async def execute_lifecycle(
        self,
        operation: str,
        plugin_name: str,
        body: dict[str, Any] | None,
        request: Request | None,
        *,
        idempotency_key: str,
    ) -> PluginLifecycleExecutionResult:
        """Execute one high-risk lifecycle intent with a durable replay record."""

        action = str(operation or "").strip().lower()
        if action not in _PLUGIN_LIFECYCLE_OPERATIONS:
            raise HTTPException(status_code=400, detail="invalid_plugin_lifecycle_operation")
        normalized_name = str(plugin_name or "").strip()
        if not normalized_name or len(normalized_name) > 128:
            raise HTTPException(status_code=400, detail="invalid_plugin_name")
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_key or len(normalized_key) > 128:
            raise HTTPException(status_code=400, detail="valid_idempotency_key_required")

        # Reject a disabled control plane before loading mutable catalog state
        # or touching the lifecycle ledger/DB pool. Production never queues a
        # lifecycle request merely to reject it inside the eventual action.
        self._require_dynamic_mutation_allowed()

        payload = _canonical_lifecycle_body(body or {})
        marketplace_item = self._lifecycle_marketplace_item(
            action,
            normalized_name,
            payload,
        )
        generation_contract = (
            marketplace_item.as_manifest_dict()
            if marketplace_item is not None
            else None
        )
        request_fingerprint = _plugin_lifecycle_fingerprint(
            action,
            normalized_name,
            payload,
            generation_contract=generation_contract,
        )
        key_hash = hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()
        claim_token = uuid.uuid4().hex
        wait_deadline = time.monotonic() + _PLUGIN_LIFECYCLE_WAIT_SECONDS

        while True:
            claim = await self.state_store.claim_lifecycle_operation(
                idempotency_key_hash=key_hash,
                request_fingerprint=request_fingerprint,
                operation=action,
                plugin_name=normalized_name,
                claim_token=claim_token,
                lease_seconds=_PLUGIN_LIFECYCLE_LEASE_SECONDS,
            )
            durable_operation = claim.operation
            if claim.plugin_busy:
                raise HTTPException(
                    status_code=409,
                    detail="plugin_lifecycle_operation_in_progress",
                    headers={"Retry-After": "1"},
                )
            if durable_operation is None:
                raise RuntimeError("plugin_lifecycle_claim_missing")
            if (
                durable_operation.request_fingerprint != request_fingerprint
                or durable_operation.operation != action
                or durable_operation.plugin_name != normalized_name
            ):
                raise HTTPException(
                    status_code=409,
                    detail="plugin_lifecycle_idempotency_conflict",
                )
            if durable_operation.status == PLUGIN_LIFECYCLE_COMPLETED:
                return self._decode_lifecycle_result(durable_operation, replayed=True)
            if claim.claimed:
                break
            if time.monotonic() >= wait_deadline:
                raise HTTPException(
                    status_code=409,
                    detail="plugin_lifecycle_operation_in_progress",
                    headers={"Retry-After": "1"},
                )
            await asyncio.sleep(0.025)

        before_state = durable_operation.before_state
        if before_state is None:
            before_state = await self._lifecycle_state_snapshot(normalized_name)
            durable_operation = await self.state_store.record_lifecycle_before_state(
                idempotency_key_hash=key_hash,
                claim_token=claim_token,
                before_state=before_state,
            )
            before_state = durable_operation.before_state or before_state

        try:
            response_payload: dict[str, Any] | None = None
            if durable_operation.attempt_count > 1:
                async with self._lifecycle_execution_fence(
                    idempotency_key_hash=key_hash,
                    claim_token=claim_token,
                ):
                    response_payload = await self._recover_lifecycle_response(
                        action,
                        normalized_name,
                        payload,
                        before_state,
                        marketplace_item=marketplace_item,
                    )
            if response_payload is None:
                response_payload = await self._run_lifecycle_action_with_lease(
                    action,
                    normalized_name,
                    payload,
                    request,
                    idempotency_key_hash=key_hash,
                    claim_token=claim_token,
                    marketplace_item=marketplace_item,
                )
            after_state = await self._lifecycle_state_snapshot(normalized_name)
            completed = await self.state_store.complete_lifecycle_operation(
                idempotency_key_hash=key_hash,
                claim_token=claim_token,
                result={"kind": "success", "response": response_payload},
                after_state=after_state,
            )
        except HTTPException as exc:
            after_state = await self._lifecycle_state_snapshot(normalized_name)
            try:
                await self.state_store.complete_lifecycle_operation(
                    idempotency_key_hash=key_hash,
                    claim_token=claim_token,
                    result={
                        "kind": "http_error",
                        "status_code": int(exc.status_code),
                        "detail": _json_safe(exc.detail),
                        "headers": dict(exc.headers or {}),
                    },
                    after_state=after_state,
                )
            except Exception as completion_error:
                await self.state_store.release_lifecycle_claim(
                    idempotency_key_hash=key_hash,
                    claim_token=claim_token,
                    error_code=type(completion_error).__name__,
                )
                raise
            raise
        except Exception as exc:
            await self.state_store.release_lifecycle_claim(
                idempotency_key_hash=key_hash,
                claim_token=claim_token,
                error_code=type(exc).__name__,
            )
            raise

        return self._decode_lifecycle_result(completed, replayed=False)

    async def _run_lifecycle_action_with_lease(
        self,
        operation: str,
        plugin_name: str,
        body: dict[str, Any],
        request: Request | None,
        *,
        idempotency_key_hash: str,
        claim_token: str,
        marketplace_item: MarketplaceItem | None = None,
    ) -> dict[str, Any]:
        """Renew the distributed claim while a bounded lifecycle action runs."""

        async def run_action() -> dict[str, Any]:
            token = _ACTIVE_PLUGIN_LIFECYCLE_CLAIM.set(
                (idempotency_key_hash, claim_token)
            )
            item_token = _ACTIVE_PLUGIN_MARKETPLACE_ITEM.set(marketplace_item)
            try:
                return await self._run_lifecycle_action(
                    operation,
                    plugin_name,
                    body,
                    request,
                )
            finally:
                _ACTIVE_PLUGIN_MARKETPLACE_ITEM.reset(item_token)
                _ACTIVE_PLUGIN_LIFECYCLE_CLAIM.reset(token)

        async with self._lifecycle_execution_fence(
            idempotency_key_hash=idempotency_key_hash,
            claim_token=claim_token,
        ):
            action_task = asyncio.create_task(run_action())
            renewal_interval = max(0.025, _PLUGIN_LIFECYCLE_LEASE_SECONDS / 3)
            try:
                while True:
                    done, _pending = await asyncio.wait(
                        {action_task},
                        timeout=renewal_interval,
                    )
                    if action_task in done:
                        return await action_task
                    renewed = await self.state_store.renew_lifecycle_claim(
                        idempotency_key_hash=idempotency_key_hash,
                        claim_token=claim_token,
                        lease_seconds=_PLUGIN_LIFECYCLE_LEASE_SECONDS,
                    )
                    if not renewed:
                        raise RuntimeError("plugin_lifecycle_claim_lost")
            finally:
                if not action_task.done():
                    action_task.cancel()
                    await _settle_under_cancellation(
                        asyncio.gather(action_task, return_exceptions=True),
                        label="lifecycle_action_cancellation",
                    )

    @asynccontextmanager
    async def _lifecycle_execution_fence(
        self,
        *,
        idempotency_key_hash: str,
        claim_token: str,
    ) -> AsyncIterator[None]:
        fence = getattr(self.state_store, "lifecycle_execution_fence", None)
        if not callable(fence):
            yield
            return
        async with fence(
            idempotency_key_hash=idempotency_key_hash,
            claim_token=claim_token,
            lease_seconds=_PLUGIN_LIFECYCLE_LEASE_SECONDS,
        ):
            yield

    async def _renew_active_lifecycle_claim(self) -> None:
        claim = _ACTIVE_PLUGIN_LIFECYCLE_CLAIM.get()
        if claim is None:
            return
        idempotency_key_hash, claim_token = claim
        renewed = await self.state_store.renew_lifecycle_claim(
            idempotency_key_hash=idempotency_key_hash,
            claim_token=claim_token,
            lease_seconds=_PLUGIN_LIFECYCLE_LEASE_SECONDS,
        )
        if not renewed:
            raise RuntimeError("plugin_lifecycle_claim_lost")

    async def marketplace(self) -> dict[str, Any]:
        manifest = self._load_manifest()
        states = {state.plugin_name: state for state in await self.state_store.list_states()}
        return {
            "items": [self._marketplace_payload(item, states.get(item.name)) for item in manifest.items],
            "restart_required": any(state.restart_required for state in states.values()),
        }

    async def install_preview(self, body: dict[str, Any]) -> dict[str, Any]:
        item = self._manifest_item(str(body.get("name") or ""))
        state = await self.state_store.get(item.name)
        return self._preview_payload(item, state)

    async def upgrade_preview(self, name: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        _ = body
        item = self._manifest_item(name)
        state = await self.state_store.get(item.name)
        if state is None or not state.installed:
            raise HTTPException(status_code=404, detail="plugin_not_installed")
        return self._preview_payload(item, state)

    async def install(self, body: dict[str, Any], request: Request | None = None) -> dict[str, Any]:
        self._require_dynamic_mutation_allowed()
        item = self._manifest_item(str(body.get("name") or ""))
        self._require_installable_item(item)
        current_state = await self.state_store.get(item.name)
        if current_state is not None and current_state.installed:
            if current_state.version == item.version:
                return {"plugin": self._installed_payload(current_state)}
            raise HTTPException(status_code=409, detail="plugin_already_installed")
        if await self.state_store.has_pending_restart():
            raise HTTPException(status_code=409, detail="plugin_restart_required")
        self._require_permission_confirmation(item, body)
        await self._require_dependencies_installed(item)
        await self._append_event(item.name, "install_requested", request=request)
        artifact_transaction: _ArtifactInstallTransaction | None = None
        try:
            artifact_transaction = (
                await self._prepare_package_artifact_async(item)
                if item.package.type == "local_archive"
                else None
            )
            if artifact_transaction is not None:
                await self._renew_active_lifecycle_claim()
                artifact_transaction.activate()
            artifact_metadata = (
                artifact_transaction.metadata
                if artifact_transaction is not None
                else {}
            )
            state = await self.state_store.upsert_marketplace_install(
                plugin_name=item.name,
                version=item.version,
                source=item.source,
                system=item.name in PLUGIN_SYSTEM_NAMES,
                metadata={**self._marketplace_metadata(item), **artifact_metadata},
            )
            if state is None:
                raise RuntimeError("plugin_state_write_missing")
        except BaseException as exc:
            if artifact_transaction is not None:
                await self._settle_failed_artifact(artifact_transaction, item)
            if not isinstance(exc, asyncio.CancelledError):
                await self._append_event(
                    item.name,
                    "install_failed",
                    status="failed",
                    message=str(exc),
                    request=request,
                )
            raise
        if artifact_transaction is not None:
            try:
                await _settle_under_cancellation(
                    asyncio.to_thread(artifact_transaction.commit),
                    label="install_artifact_commit",
                )
            except Exception:
                # The durable row and target artifact already agree. Leaving a
                # private backup for later GC is safer than restoring stale code.
                logger.exception(
                    "plugin.local_archive_finalize_failed",
                    plugin=item.name,
                )
        await self._append_event(item.name, "install_succeeded", request=request)
        return {"plugin": self._installed_payload(state)}

    async def upgrade(
        self,
        name: str,
        body: dict[str, Any] | None = None,
        request: Request | None = None,
    ) -> dict[str, Any]:
        self._require_dynamic_mutation_allowed()
        payload = body or {}
        item = self._manifest_item(name)
        self._require_installable_item(item)
        state = await self._state_or_404(item.name)
        if not state.installed:
            raise HTTPException(status_code=404, detail="plugin_not_installed")
        if state.restart_required:
            raise HTTPException(status_code=409, detail="plugin_restart_required")
        try:
            if Version(item.version) <= Version(state.version):
                raise HTTPException(status_code=409, detail="target_version_not_newer")
        except InvalidVersion as exc:
            raise HTTPException(status_code=409, detail="invalid_plugin_version") from exc
        await self._reject_incompatible_enabled_dependents(item)
        if await self.state_store.has_pending_restart(exclude_plugin_name=item.name):
            raise HTTPException(status_code=409, detail="plugin_restart_required")
        self._require_permission_confirmation(item, payload)
        await self._require_dependencies_installed(item)
        await self._append_event(item.name, "upgrade_requested", request=request)
        artifact_transaction: _ArtifactInstallTransaction | None = None
        try:
            artifact_transaction = (
                await self._prepare_package_artifact_async(item)
                if item.package.type == "local_archive"
                else None
            )
            if artifact_transaction is not None:
                await self._renew_active_lifecycle_claim()
                artifact_transaction.activate()
            artifact_metadata = (
                artifact_transaction.metadata
                if artifact_transaction is not None
                else {}
            )
            next_state = await self.state_store.mark_upgraded(
                plugin_name=item.name,
                version=item.version,
                source=item.source,
                metadata={**self._marketplace_metadata(item), **artifact_metadata},
            )
            if next_state is None:
                raise RuntimeError("plugin_state_write_missing")
        except BaseException as exc:
            if artifact_transaction is not None:
                await self._settle_failed_artifact(artifact_transaction, item)
            if not isinstance(exc, asyncio.CancelledError):
                await self._append_event(
                    item.name,
                    "upgrade_failed",
                    status="failed",
                    message=str(exc),
                    request=request,
                )
            raise
        if artifact_transaction is not None:
            try:
                await _settle_under_cancellation(
                    asyncio.to_thread(artifact_transaction.commit),
                    label="upgrade_artifact_commit",
                )
            except Exception:
                logger.exception(
                    "plugin.local_archive_finalize_failed",
                    plugin=item.name,
                )
        await self._append_event(item.name, "upgrade_succeeded", request=request)
        return {"plugin": self._installed_payload(next_state)}

    async def uninstall(self, name: str, request: Request | None = None) -> dict[str, Any]:
        self._require_dynamic_mutation_allowed()
        plugin_name = self._validate_request_name(name)
        state = await self._state_or_404(plugin_name)
        if state.system:
            await self._append_event(
                plugin_name,
                "uninstall",
                status="rejected",
                message="system_plugin_cannot_be_uninstalled",
                request=request,
            )
            raise HTTPException(status_code=409, detail="system_plugin_cannot_be_uninstalled")
        if await self.state_store.has_pending_restart():
            raise HTTPException(status_code=409, detail="plugin_restart_required")
        await self._reject_installed_dependents(plugin_name)
        await self._append_event(plugin_name, "uninstall_requested", request=request)
        next_state = await self.state_store.mark_uninstalled(plugin_name)
        await self._append_event(plugin_name, "uninstall_succeeded", request=request)
        return {"plugin": self._installed_payload(next_state or state)}

    async def restart_instructions(self) -> dict[str, Any]:
        return {
            "actionable": False,
            "restart_required": await self.state_store.has_pending_restart(),
            "message": "Restart the FastAPI process or container through the deployment system.",
        }

    async def config_schema(self, name: str) -> dict[str, Any]:
        self._plugin_or_404(name)
        state = await self._state_or_404(name)
        return {
            "plugin_name": name,
            "schema": self._cached_config_schema(name, state),
            "admin_ui": self._cached_admin_ui(name, state),
        }

    async def runtime(self, name: str) -> dict[str, Any]:
        plugin = self._plugin_or_404(name)
        state = await self.state_store.get(name)
        allowed_lookup = getattr(self.registry, "global_execution_allowed", None)
        allowed = bool(
            await allowed_lookup(name)
            if callable(allowed_lookup)
            else state is not None and state.installed and state.enabled
        )
        runtime = (
            await plugin.get_runtime_status()
            if allowed
            else {"running": False, "execution_allowed": False}
        )
        return {
            "plugin_name": name,
            "state": state.as_dict() if state is not None else None,
            "runtime_status": runtime,
            "runtime": runtime,
        }

    async def events(
        self,
        *,
        plugin_name: str = "",
        event_type: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        if plugin_name:
            self._validate_request_name(plugin_name)
        rows = await self.state_store.list_events(
            plugin_name=plugin_name,
            event_type=event_type.strip(),
            limit=limit,
            offset=offset,
        )
        return {"events": [row.as_dict() for row in rows]}

    async def scope_states(
        self,
        *,
        tenant_id: str,
        session_id: str | None = None,
        plugin_name: str = "",
    ) -> dict[str, Any]:
        tenant = tenant_id.strip()
        if not tenant:
            raise HTTPException(status_code=400, detail="tenant_id_required")
        if plugin_name:
            self._validate_request_name(plugin_name)
        rows = await self.state_store.list_scope_states(
            tenant_id=tenant,
            session_id=(session_id or None),
            plugin_name=plugin_name,
        )
        return {"items": [row.as_dict() for row in rows]}

    async def set_scope_state(
        self,
        name: str,
        body: dict[str, Any],
        request: Request | None = None,
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        plugin_name = self._validate_request_name(name)
        self._plugin_or_404(plugin_name)
        state = await self._state_or_404(plugin_name)
        if not state.installed:
            raise HTTPException(status_code=404, detail="plugin_not_installed")
        tenant_id = str(body.get("tenant_id") or "").strip()
        if not tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id_required")
        session_id = str(body.get("session_id") or "").strip()
        enabled = bool(body.get("enabled"))
        config = body.get("config") if isinstance(body.get("config"), dict) else {}
        try:
            validate_plugin_config(
                config,
                self._cached_config_schema(plugin_name, state),
            )
        except PluginConfigSchemaError as exc:
            raise HTTPException(status_code=503, detail=exc.as_detail()) from exc
        except PluginConfigValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.as_detail()) from exc
        scope_state = await self.state_store.set_scope_enabled(
            tenant_id=tenant_id,
            session_id=session_id,
            plugin_name=plugin_name,
            enabled=enabled,
            expected_version=expected_version,
            config=config,
        )
        await self._append_event(
            plugin_name,
            "scope_enable" if enabled else "scope_disable",
            request=request,
            metadata={"tenant_id": tenant_id, "session_id": session_id},
        )
        return {"scope_state": scope_state.as_dict() if scope_state is not None else None}

    async def enable(self, name: str, request: Request | None = None) -> dict[str, Any]:
        self._require_dynamic_mutation_allowed()
        plugin_name = self._validate_request_name(name)
        state = await self._state_or_404(plugin_name)
        if not state.installed:
            raise HTTPException(status_code=404, detail="plugin_not_installed")
        if state.restart_required:
            raise HTTPException(status_code=409, detail="plugin_restart_required")
        pinned_item = _ACTIVE_PLUGIN_MARKETPLACE_ITEM.get()
        manifest_item = (
            pinned_item
            if pinned_item is not None and pinned_item.name == plugin_name
            else self._load_manifest().by_name().get(plugin_name)
        )
        if (
            manifest_item is None
            and plugin_name not in self.registry.loaded_plugins
        ):
            raise HTTPException(status_code=404, detail="plugin_not_found")
        await self._require_dependency_contract(
            self._installed_dependencies(
                state,
                fallback_item=manifest_item,
            )
        )
        await self._append_event(plugin_name, "enable_requested", request=request)
        initialized_now = False
        restart_required = True

        # Phase 1 records desired enablement behind a restart fence. The
        # durable execution gate therefore stays closed while on_enable and
        # owner-bound catalogs are being published locally.
        next_state = await self.state_store.set_enabled(
            plugin_name,
            True,
            restart_required=True,
        )
        if next_state is None:
            raise RuntimeError("plugin_state_write_missing")

        if not self.registry.is_initialized(plugin_name):
            # A plugin skipped at process startup is absent from every
            # startup-only catalog. Do not import/initialize it on one replica
            # and pretend the deployment is homogeneous; enable desired state
            # behind a restart fence instead.
            initialized_now = True
        else:
            try:
                # Other replicas retain their owner-bound catalogs while the
                # gate is closed; only the replica which performed local
                # cleanup needs to republish before phase 2 opens the gate.
                await self.registry.reactivate_plugin(plugin_name, self.ctx)
            except BaseException:
                try:
                    await _settle_under_cancellation(
                        self.state_store.set_enabled(
                            plugin_name,
                            False,
                            restart_required=False,
                        ),
                        label="enable_prepare_rollback",
                    )
                except Exception:
                    logger.exception(
                        "plugin.enable_prepare_rollback_failed",
                        plugin=plugin_name,
                    )
                raise

            try:
                final_state = await self.state_store.set_enabled(
                    plugin_name,
                    True,
                    restart_required=False,
                )
                if final_state is None:
                    raise RuntimeError("plugin_state_write_missing")
                next_state = final_state
                restart_required = False
            except BaseException as activation_error:
                # A lost response may hide a committed phase-2 write. Confirm
                # before undoing local activation; otherwise deactivate so a
                # prepared-but-closed generation cannot leak background work.
                async def reconcile_activation() -> PluginState | None:
                    try:
                        observed_state = await self.state_store.get(plugin_name)
                    except Exception:
                        observed_state = None
                    committed_state = bool(
                        observed_state is not None
                        and observed_state.installed
                        and observed_state.enabled
                        and not observed_state.restart_required
                        and observed_state.version == state.version
                    )
                    if not committed_state:
                        try:
                            await self.registry.deactivate_plugin(
                                plugin_name,
                                self.ctx.container,
                            )
                        except Exception:
                            logger.exception(
                                "plugin.enable_activation_rollback_failed",
                                plugin=plugin_name,
                            )
                        return None
                    return observed_state

                observed = await _settle_under_cancellation(
                    reconcile_activation(),
                    label="enable_activation_reconcile",
                )
                committed = observed is not None
                if committed:
                    next_state = observed
                    restart_required = False
                    if isinstance(activation_error, asyncio.CancelledError):
                        raise
                else:
                    raise
        await self._append_event(
            plugin_name,
            "enable_succeeded",
            request=request,
            metadata={"initialized_now": initialized_now},
        )
        return {
            "plugin": self._installed_payload(next_state or state),
            "disable_mode": (
                "restart_required" if restart_required else "runtime_filtered"
            ),
            "restart_required": restart_required,
        }

    async def disable(self, name: str, request: Request | None = None) -> dict[str, Any]:
        self._require_dynamic_mutation_allowed()
        plugin = self._plugin_or_404(name)
        state = await self._state_or_404(name)
        if not state.installed:
            raise HTTPException(status_code=404, detail="plugin_not_installed")
        if state.restart_required:
            raise HTTPException(status_code=409, detail="plugin_restart_required")
        if state.system:
            await self._append_event(
                name,
                "disable_rejected",
                status="rejected",
                message="system_plugin_cannot_be_disabled",
                request=request,
            )
            raise HTTPException(status_code=409, detail="system_plugin_cannot_be_disabled")
        await self._reject_enabled_dependents(name)
        await self._append_event(name, "disable_requested", request=request)
        restart_required = self._has_static_runtime_contributions(plugin)
        # Durable state is the cross-replica execution gate and must close
        # before best-effort in-process cleanup begins.
        next_state = await self.state_store.set_enabled(
            name,
            False,
            restart_required=restart_required,
        )
        cleanup = await self.registry.deactivate_plugin(name, self.ctx.container)
        cleanup_errors = max(0, int(cleanup.get("cleanup_errors") or 0))
        if cleanup_errors:
            # The cross-replica execution gate remains closed, but autonomous
            # resources may still be alive in this process. Never advertise a
            # successful hot disable when cleanup could not be proven.
            restart_required = True
            fenced_state = await self.state_store.set_enabled(
                name,
                False,
                restart_required=True,
            )
            if fenced_state is not None:
                next_state = fenced_state
        disable_mode = "restart_required" if restart_required else "runtime_filtered"
        await self._append_event(
            name,
            "disable_partial" if cleanup_errors else "disable_succeeded",
            status="partial" if cleanup_errors else "ok",
            message="plugin_disable_cleanup_incomplete" if cleanup_errors else "",
            request=request,
            metadata={"cleanup": cleanup, "disable_mode": disable_mode},
        )
        return {
            "plugin": self._installed_payload(next_state or state),
            "cleanup": cleanup,
            "disable_mode": disable_mode,
            "restart_required": restart_required,
            "cleanup_partial": bool(cleanup_errors),
        }

    def _has_static_runtime_contributions(self, plugin: Any) -> bool:
        """Return whether a restart is needed to remove catalog structure."""

        try:
            descriptor = self.registry.descriptor(plugin.meta.name)
            if descriptor is not None:
                return bool(
                    descriptor.admin_routes
                    or descriptor.capability_engines
                    or descriptor.flow_steps
                    or descriptor.effects
                    or descriptor.admin_media_providers
                    or descriptor.channel_adapters
                )
            return bool(
                plugin.get_api_router()
                or plugin.get_capability_engines()
                or plugin.get_flow_steps()
                or plugin.get_flow_executors()
                or plugin.get_effect_handlers()
                or plugin.get_admin_media_event_provider()
                or plugin.get_channel_adapters()
            )
        except Exception:
            # A contribution getter failing during a safety transition must
            # never turn into an optimistic hot-unload claim.
            return True

    def _decode_lifecycle_result(
        self,
        operation: PluginLifecycleOperation,
        *,
        replayed: bool,
    ) -> PluginLifecycleExecutionResult:
        result = operation.result
        if not isinstance(result, dict):
            raise RuntimeError("plugin_lifecycle_result_missing")
        kind = str(result.get("kind") or "")
        if kind == "http_error":
            raw_headers = result.get("headers")
            headers = (
                {str(key): str(value) for key, value in raw_headers.items()}
                if isinstance(raw_headers, dict)
                else {}
            )
            if replayed:
                headers["Idempotent-Replayed"] = "true"
            raise HTTPException(
                status_code=max(400, int(result.get("status_code") or 500)),
                detail=result.get("detail") or "plugin_lifecycle_operation_failed",
                headers=headers or None,
            )
        response = result.get("response")
        if kind != "success" or not isinstance(response, dict):
            raise RuntimeError("plugin_lifecycle_result_corrupt")
        return PluginLifecycleExecutionResult(
            response=dict(response),
            before_state=dict(operation.before_state or {}),
            after_state=dict(operation.after_state or {}),
            policy_version=max(0, int(operation.policy_version)),
            idempotent_replayed=replayed,
        )

    async def _run_lifecycle_action(
        self,
        operation: str,
        plugin_name: str,
        body: dict[str, Any],
        request: Request | None,
    ) -> dict[str, Any]:
        if operation == "install":
            return await self.install(body, request)
        if operation == "upgrade":
            return await self.upgrade(plugin_name, body, request)
        if operation == "uninstall":
            return await self.uninstall(plugin_name, request)
        if operation == "enable":
            return await self.enable(plugin_name, request)
        if operation == "disable":
            return await self.disable(plugin_name, request)
        raise HTTPException(status_code=400, detail="invalid_plugin_lifecycle_operation")

    async def _recover_lifecycle_response(
        self,
        operation: str,
        plugin_name: str,
        body: dict[str, Any],
        before_state: dict[str, Any],
        *,
        marketplace_item: MarketplaceItem | None = None,
    ) -> dict[str, Any] | None:
        """Reconcile an ambiguous attempt without repeating an observed side effect."""

        state = await self.state_store.get(plugin_name)
        if operation in {"install", "upgrade"}:
            item = marketplace_item or self._manifest_item(plugin_name)
            state_was_target = (
                bool(before_state.get("installed"))
                and str(before_state.get("version") or "") == item.version
            )
            if (
                state is not None
                and state.installed
                and state.version == item.version
                and (operation == "install" or not state_was_target)
            ):
                return {"plugin": self._installed_payload(state)}
            return None

        if operation == "uninstall":
            if (
                state is not None
                and not state.installed
                and bool(before_state.get("installed"))
            ):
                return {"plugin": self._installed_payload(state)}
            return None

        if (
            operation == "enable"
            and state is not None
            and state.enabled
            and not bool(before_state.get("enabled"))
        ):
            return {
                "plugin": self._installed_payload(state),
                "disable_mode": (
                    "restart_required"
                    if state.restart_required
                    else "runtime_filtered"
                ),
                "restart_required": state.restart_required,
            }

        if (
            operation == "disable"
            and state is not None
            and state.installed
            and not state.enabled
            and bool(before_state.get("enabled"))
        ):
            runtime_active, _runtime_initialized = self._runtime_lifecycle_flags(
                plugin_name
            )
            if not runtime_active:
                restart_required = state.restart_required
                return {
                    "plugin": self._installed_payload(state),
                    "cleanup": {"hooks": 0, "agent_tools": 0, "commands": 0},
                    "disable_mode": (
                        "restart_required"
                        if restart_required
                        else "runtime_filtered"
                    ),
                    "restart_required": restart_required,
                    "cleanup_partial": False,
                }

            # The durable gate closed before the previous process/request was
            # interrupted, but this replica still exposes contributions.
            # Finish local cleanup without replaying the state transition.
            plugin = self._plugin_or_404(plugin_name)
            restart_required = state.restart_required or self._has_static_runtime_contributions(
                plugin
            )
            cleanup = await self.registry.deactivate_plugin(
                plugin_name,
                self.ctx.container,
            )
            cleanup_errors = max(0, int(cleanup.get("cleanup_errors") or 0))
            if cleanup_errors and not state.restart_required:
                restart_required = True
                state = await self.state_store.set_enabled(
                    plugin_name,
                    False,
                    restart_required=True,
                ) or state
            return {
                "plugin": self._installed_payload(state),
                "cleanup": cleanup,
                "disable_mode": (
                    "restart_required" if restart_required else "runtime_filtered"
                ),
                "restart_required": restart_required,
                "cleanup_partial": bool(cleanup_errors),
            }

        plugin = self._plugin_or_404(plugin_name)
        runtime_active, _runtime_initialized = self._runtime_lifecycle_flags(plugin_name)
        has_static_runtime = self._has_static_runtime_contributions(plugin)
        if operation == "enable" and runtime_active and state is not None:
            initialized_before = bool(before_state.get("runtime_initialized"))
            restart_required = not initialized_before
            if not state.enabled or state.restart_required != restart_required:
                state = await self.state_store.set_enabled(
                    plugin_name,
                    True,
                    restart_required=restart_required,
                ) or state
            return {
                "plugin": self._installed_payload(state),
                "disable_mode": "runtime_filtered",
                "restart_required": restart_required,
            }

        if operation == "disable" and not runtime_active and state is not None:
            restart_required = has_static_runtime
            if state.enabled or state.restart_required != restart_required:
                state = await self.state_store.set_enabled(
                    plugin_name,
                    False,
                    restart_required=restart_required,
                ) or state
            return {
                "plugin": self._installed_payload(state),
                "cleanup": {"hooks": 0, "agent_tools": 0, "commands": 0},
                "disable_mode": "restart_required" if restart_required else "runtime_filtered",
                "restart_required": restart_required,
                "cleanup_partial": False,
            }
        _ = body
        return None

    async def _lifecycle_state_snapshot(self, plugin_name: str) -> dict[str, Any]:
        state = await self.state_store.get(plugin_name)
        runtime_active, runtime_initialized = self._runtime_lifecycle_flags(plugin_name)
        if state is None:
            return {
                "plugin_name": plugin_name,
                "exists": False,
                "runtime_active": runtime_active,
                "runtime_initialized": runtime_initialized,
            }
        return {
            "plugin_name": state.plugin_name,
            "exists": True,
            "version": state.version,
            "source": state.source,
            "installed": state.installed,
            "enabled": state.enabled,
            "system": state.system,
            "status": state.status,
            "restart_required": state.restart_required,
            "runtime_active": runtime_active,
            "runtime_initialized": runtime_initialized,
        }

    def _runtime_lifecycle_flags(self, plugin_name: str) -> tuple[bool, bool]:
        return (
            self.registry.is_active(plugin_name),
            self.registry.is_initialized(plugin_name),
        )

    def _load_manifest(self):
        path = Path(str(getattr(self.ctx.settings, "plugin_marketplace_path", "config/plugin-marketplace.yaml")))
        if not path.is_absolute():
            path = self.ctx.settings.project_root / path
        try:
            return load_marketplace_manifest(path)
        except MarketplaceManifestError as exc:
            raise HTTPException(status_code=400, detail="invalid_manifest") from exc

    def _manifest_item(self, name: str) -> MarketplaceItem:
        plugin_name = self._validate_request_name(name)
        pinned_item = _ACTIVE_PLUGIN_MARKETPLACE_ITEM.get()
        if pinned_item is not None:
            if pinned_item.name != plugin_name:
                raise HTTPException(
                    status_code=409,
                    detail="plugin_lifecycle_generation_mismatch",
                )
            return pinned_item
        item = self._load_manifest().by_name().get(plugin_name)
        if item is None:
            raise HTTPException(status_code=404, detail="marketplace_plugin_not_found")
        return item

    def _lifecycle_marketplace_item(
        self,
        operation: str,
        plugin_name: str,
        body: dict[str, Any],
    ) -> MarketplaceItem | None:
        """Pin the exact catalog generation before claiming an idempotency key."""

        # Lightweight manager harnesses intentionally omit runtime settings.
        # Production managers always have ``ctx`` and therefore bind every
        # manifest-backed lifecycle intent to an immutable catalog snapshot.
        if not hasattr(self, "ctx"):
            return None
        target_name = (
            str(body.get("name") or plugin_name).strip()
            if operation == "install"
            else plugin_name
        )
        item = self._load_manifest().by_name().get(target_name)
        if item is None and operation in {"install", "upgrade"}:
            raise HTTPException(status_code=404, detail="marketplace_plugin_not_found")
        return item

    def _plugin_or_404(self, name: str):
        plugin = self.registry.loaded_plugins.get(name)
        if plugin is None:
            raise HTTPException(status_code=404, detail="plugin_not_found")
        return plugin

    async def _state_or_404(self, name: str) -> PluginState:
        state = await self.state_store.get(name)
        if state is None:
            raise HTTPException(status_code=404, detail="plugin_state_not_found")
        return state

    def _installed_payload(self, state: PluginState) -> dict[str, Any]:
        plugin = self.registry.loaded_plugins.get(state.plugin_name)
        payload = state.as_dict()
        if plugin is not None:
            descriptor_lookup = getattr(self.registry, "descriptor", None)
            descriptor = (
                descriptor_lookup(state.plugin_name)
                if callable(descriptor_lookup)
                else None
            )
            payload["description"] = (
                descriptor.description
                if descriptor
                else str(state.metadata.get("description") or plugin.meta.description)
            )
            payload["dependencies"] = list(
                descriptor.dependencies
                if descriptor
                else state.metadata.get("dependencies") or plugin.meta.dependencies
            )
            payload["permissions"] = list(
                descriptor.permissions
                if descriptor
                else state.metadata.get("permissions") or []
            )
            payload["has_router"] = (
                bool(descriptor.admin_routes)
                if descriptor
                else plugin.get_api_router() is not None
            )
            payload["has_capability_engine"] = bool(
                descriptor.capability_engines if descriptor else False
            )
            payload["capabilities"] = (
                descriptor.as_capabilities() if descriptor is not None else {}
            )
            payload["capability_source"] = (
                "runtime_descriptor" if descriptor is not None else "unavailable"
            )
            payload["config_schema"] = self._cached_config_schema(
                state.plugin_name,
                state,
            )
            payload["admin_ui"] = self._cached_admin_ui(
                state.plugin_name,
                state,
            )
        else:
            metadata = state.metadata
            payload["description"] = str(metadata.get("description") or "")
            payload["dependencies"] = list(metadata.get("dependencies") or [])
            payload["permissions"] = list(metadata.get("permissions") or [])
            payload["has_router"] = False
            payload["has_capability_engine"] = False
            payload["capabilities"] = {}
            payload["capability_source"] = "unavailable"
            payload["config_schema"] = {}
            payload["admin_ui"] = {}
        return payload

    def _marketplace_payload(self, item: MarketplaceItem, state: PluginState | None) -> dict[str, Any]:
        installed = bool(state and state.installed)
        warnings: list[str] = []
        if not item.compatible:
            warnings.append("incompatible_plugin_api")
        if not self._is_supported_package_type(item):
            warnings.append("unsupported_package_type")
        descriptor = None
        if item.source == "builtin" and item.package.type == "builtin":
            descriptor_lookup = getattr(self.registry, "descriptor", None)
            if callable(descriptor_lookup):
                descriptor = descriptor_lookup(item.name)
        capabilities = (
            descriptor.as_capabilities()
            if descriptor is not None
            else {key: list(value) for key, value in item.capabilities.items()}
        )
        runtime_schema = self._registry_document("config_schema", item.name)
        runtime_admin_ui = self._registry_document("admin_ui", item.name)
        return {
            "name": item.name,
            "display_name": item.display_name,
            "version": item.version,
            "description": item.description,
            "source": item.source,
            "package_type": item.package.type,
            "installed": installed,
            "installed_version": state.version if state is not None and state.installed else "",
            "enabled": bool(state.enabled) if state is not None else False,
            "compatible": item.compatible,
            "status": state.status if state is not None else "available",
            "restart_required": bool(state.restart_required) if state is not None else False,
            "permissions": [permission.as_dict() for permission in item.permissions],
            "dependencies": [dependency.as_dict() for dependency in item.dependencies],
            "capabilities": capabilities,
            "capability_source": (
                "runtime_descriptor" if descriptor is not None else "manifest"
            ),
            "config_schema": (
                runtime_schema
                if runtime_schema is not None
                else item.config_schema
            ),
            "admin_ui": (
                runtime_admin_ui
                if runtime_admin_ui is not None
                else dict((state.metadata if state is not None else {}).get("admin_ui") or {})
            ),
            "restart_policy": item.restart_policy,
            "warnings": warnings,
        }

    def _registry_document(
        self,
        method_name: str,
        plugin_name: str,
    ) -> dict[str, Any] | None:
        lookup = getattr(self.registry, method_name, None)
        value = lookup(plugin_name) if callable(lookup) else None
        return dict(value) if isinstance(value, dict) else None

    def _cached_config_schema(
        self,
        plugin_name: str,
        state: PluginState,
    ) -> dict[str, Any]:
        cached = self._registry_document("config_schema", plugin_name)
        if cached is not None:
            return cached
        manifest = state.metadata.get("manifest")
        if isinstance(manifest, dict) and isinstance(manifest.get("config_schema"), dict):
            return dict(manifest["config_schema"])
        return {}

    def _cached_admin_ui(
        self,
        plugin_name: str,
        state: PluginState,
    ) -> dict[str, Any]:
        cached = self._registry_document("admin_ui", plugin_name)
        if cached is not None:
            return cached
        value = state.metadata.get("admin_ui")
        return dict(value) if isinstance(value, dict) else {}

    def _preview_payload(self, item: MarketplaceItem, state: PluginState | None) -> dict[str, Any]:
        current_permissions = []
        if state is not None:
            current_permissions = [str(value) for value in state.metadata.get("permissions", [])]
        return {
            "name": item.name,
            "version": item.version,
            "compatible": item.compatible,
            "installed_version": state.version if state is not None and state.installed else "",
            "permission_changes": permission_delta(current_permissions, item.permission_ids),
            "restart_required": item.restart_policy != "none",
            "permissions": [permission.as_dict() for permission in item.permissions],
            "dependencies": [dependency.as_dict() for dependency in item.dependencies],
            "warnings": self._preview_warnings(item, state),
        }

    def _preview_warnings(self, item: MarketplaceItem, state: PluginState | None) -> list[str]:
        warnings: list[str] = []
        if not item.compatible:
            warnings.append("incompatible_plugin_api")
        if not self._is_supported_package_type(item):
            warnings.append("unsupported_package_type")
        if state is not None and state.installed:
            try:
                if Version(item.version) <= Version(state.version):
                    warnings.append("target_version_not_newer")
            except InvalidVersion:
                warnings.append("invalid_installed_version")
        return warnings

    def _marketplace_metadata(self, item: MarketplaceItem) -> dict[str, Any]:
        return {
            "description": item.description,
            "display_name": item.display_name,
            "dependencies": [dependency.as_dict() for dependency in item.dependencies],
            "permissions": item.permission_ids,
            "manifest": item.as_manifest_dict(),
        }

    def _require_installable_item(self, item: MarketplaceItem) -> None:
        if not self._is_supported_package_type(item):
            raise HTTPException(status_code=422, detail="unsupported_package_type")
        if item.package.type == "local_archive":
            self._require_valid_local_archive(item)
            if not item.capability_digest:
                raise HTTPException(
                    status_code=422,
                    detail="plugin_capability_digest_required",
                )
        if not item.compatible:
            raise HTTPException(status_code=422, detail="incompatible_plugin_api")

    def _require_dynamic_mutation_allowed(self) -> None:
        settings = self.ctx.settings
        allowed = getattr(settings, "allow_dynamic_plugin_mutations", None)
        if allowed is None:
            environment = str(getattr(settings, "app_env", "dev") or "dev").lower()
            enabled = bool(getattr(settings, "plugin_dynamic_mutations_enabled", True))
            allowed = enabled and environment in {"dev", "test"}
        if not bool(allowed):
            raise HTTPException(
                status_code=403,
                detail="dynamic_plugin_mutations_disabled",
            )
        project_root = Path(settings.project_root).resolve()
        configured_install_dir = Path(
            str(getattr(settings, "plugin_install_dir", "") or "")
        )
        if not configured_install_dir.is_absolute():
            configured_install_dir = project_root / configured_install_dir
        if configured_install_dir.resolve() == (project_root / "plugins").resolve():
            raise HTTPException(
                status_code=409,
                detail="plugin_install_dir_must_be_separate_from_builtins",
            )

    def _is_supported_package_type(self, item: MarketplaceItem) -> bool:
        return item.package.type in {"builtin", "local_archive"}

    def _require_valid_local_archive(self, item: MarketplaceItem) -> None:
        uri = item.package.uri.strip()
        if not uri:
            raise HTTPException(status_code=422, detail="local_archive_uri_required")
        posix_path = PurePosixPath(uri.replace("\\", "/"))
        windows_path = PureWindowsPath(uri)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or ".." in posix_path.parts
            or ".." in windows_path.parts
        ):
            raise HTTPException(status_code=422, detail="invalid_local_archive_uri")
        if posix_path.suffix.lower() != ".zip":
            raise HTTPException(status_code=422, detail="invalid_local_archive_uri")
        if not item.package.checksum.strip():
            raise HTTPException(status_code=422, detail="local_archive_checksum_required")

    async def _prepare_package_artifact_async(
        self,
        item: MarketplaceItem,
    ) -> _ArtifactInstallTransaction:
        prepare_task = asyncio.create_task(
            asyncio.to_thread(self._prepare_package_artifact, item)
        )
        cancellation_requested = False
        while not prepare_task.done():
            try:
                await asyncio.shield(prepare_task)
            except asyncio.CancelledError:
                cancellation_requested = True
        try:
            transaction = prepare_task.result()
        except BaseException as exc:
            if cancellation_requested:
                raise asyncio.CancelledError() from exc
            raise
        if cancellation_requested:
            await self._settle_failed_artifact(transaction, item)
            raise asyncio.CancelledError()
        return transaction

    def _prepare_package_artifact(
        self,
        item: MarketplaceItem,
    ) -> _ArtifactInstallTransaction:
        archive_path = self._resolve_local_archive_path(item)
        checksum = self._require_sha256_checksum(item.package.checksum)

        install_root = self._plugin_install_root()
        target_dir = install_root / item.name
        installed_path = self._display_path(target_dir)
        staging_root = install_root / ".plugin-install-staging"
        staging_dir = staging_root / f"{item.name}-{uuid.uuid4().hex}"
        backup_dir = staging_root / f"{item.name}-backup-{uuid.uuid4().hex}"
        archive_snapshot = staging_root / f"{item.name}-archive-{uuid.uuid4().hex}.zip"
        staging_root.mkdir(parents=True, exist_ok=True)
        try:
            actual_checksum = self._snapshot_local_archive(
                archive_path,
                archive_snapshot,
            )
            if actual_checksum != checksum:
                raise HTTPException(
                    status_code=422,
                    detail="local_archive_checksum_mismatch",
                )
            staging_dir.mkdir(parents=True)
            self._extract_local_archive(archive_snapshot, staging_dir, item)
        except HTTPException:
            _remove_local_path(staging_dir)
            raise
        except Exception as exc:
            _remove_local_path(staging_dir)
            raise HTTPException(status_code=500, detail="local_archive_install_failed") from exc
        finally:
            archive_snapshot.unlink(missing_ok=True)
        return _ArtifactInstallTransaction(
            metadata={
                "artifact": {
                    "package_type": item.package.type,
                    "checksum": item.package.checksum,
                    "installed_path": installed_path,
                    "tree_digest": compute_plugin_tree_digest(staging_dir),
                }
            },
            target_dir=target_dir,
            staging_dir=staging_dir,
            backup_dir=backup_dir,
        )

    async def _settle_failed_artifact(
        self,
        transaction: _ArtifactInstallTransaction,
        item: MarketplaceItem,
    ) -> None:
        """Resolve the filesystem half after a possibly ambiguous DB write."""

        async def settle() -> None:
            if not transaction.activated:
                try:
                    await asyncio.to_thread(transaction.rollback)
                except Exception:
                    logger.exception(
                        "plugin.local_archive_rollback_failed",
                        plugin=item.name,
                    )
                return

            try:
                state = await self.state_store.get(item.name)
            except Exception:
                # The DB may have committed before the client observed a
                # transport failure. Preserve target and backup for
                # deterministic operator/retry recovery instead of guessing.
                logger.exception(
                    "plugin.local_archive_state_ambiguous",
                    plugin=item.name,
                )
                return

            artifact = (
                state.metadata.get("artifact") if state is not None else None
            )
            durable_matches = bool(
                state is not None
                and state.installed
                and state.version == item.version
                and isinstance(artifact, dict)
                and str(artifact.get("checksum") or "") == item.package.checksum
            )
            try:
                if durable_matches:
                    await asyncio.to_thread(transaction.commit)
                else:
                    await asyncio.to_thread(transaction.rollback)
            except Exception:
                logger.exception(
                    "plugin.local_archive_settlement_failed",
                    plugin=item.name,
                    durable_matches=durable_matches,
                )

        await _settle_under_cancellation(
            settle(),
            label="artifact_failure_settlement",
        )

    def _resolve_local_archive_path(self, item: MarketplaceItem) -> Path:
        project_root = Path(self.ctx.settings.project_root).resolve()
        archive_path = (project_root / item.package.uri.strip()).resolve()
        if not archive_path.is_relative_to(project_root):
            raise HTTPException(status_code=422, detail="invalid_local_archive_uri")
        if not archive_path.is_file():
            raise HTTPException(status_code=422, detail="local_archive_not_found")
        return archive_path

    def _plugin_install_root(self) -> Path:
        raw = str(getattr(self.ctx.settings, "plugin_install_dir", "plugins"))
        install_root = Path(raw)
        if not install_root.is_absolute():
            install_root = self.ctx.settings.project_root / install_root
        install_root.mkdir(parents=True, exist_ok=True)
        return install_root

    def _display_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.ctx.settings.project_root).as_posix()
        except ValueError:
            return path.as_posix()

    def _require_sha256_checksum(self, checksum: str) -> str:
        value = checksum.strip().lower()
        if value.startswith("sha256:"):
            value = value.removeprefix("sha256:")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise HTTPException(status_code=422, detail="invalid_local_archive_checksum")
        return value

    def _snapshot_local_archive(self, source_path: Path, snapshot_path: Path) -> str:
        """Copy and hash one immutable package generation before validation."""

        digest = hashlib.sha256()
        written = 0
        with source_path.open("rb") as source, snapshot_path.open("xb") as snapshot:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                written += len(chunk)
                if written > _LOCAL_ARCHIVE_MAX_FILE_BYTES:
                    raise HTTPException(
                        status_code=422,
                        detail="local_archive_file_too_large",
                    )
                digest.update(chunk)
                snapshot.write(chunk)
        return digest.hexdigest()

    def _extract_local_archive(
        self,
        archive_path: Path,
        target_dir: Path,
        item: MarketplaceItem,
    ) -> None:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members = self._validate_zip_archive(archive)
                total_written = 0
                for info, normalized_name in members:
                    output_path = target_dir.joinpath(*PurePosixPath(normalized_name).parts)
                    if info.is_dir():
                        output_path.mkdir(parents=True, exist_ok=True)
                        continue
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    member_written = 0
                    with archive.open(info, "r") as source, output_path.open("xb") as target:
                        while chunk := source.read(1024 * 1024):
                            member_written += len(chunk)
                            total_written += len(chunk)
                            if member_written > _LOCAL_ARCHIVE_MAX_MEMBER_BYTES:
                                raise HTTPException(
                                    status_code=422,
                                    detail="local_archive_member_too_large",
                                )
                            if total_written > _LOCAL_ARCHIVE_MAX_TOTAL_BYTES:
                                raise HTTPException(
                                    status_code=422,
                                    detail="local_archive_uncompressed_size_exceeded",
                                )
                            target.write(chunk)
                self._validate_staged_plugin(target_dir, item)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail="invalid_local_archive") from exc

    def _validate_zip_archive(
        self,
        archive: zipfile.ZipFile,
    ) -> list[tuple[zipfile.ZipInfo, str]]:
        infos = archive.infolist()
        if not infos:
            raise HTTPException(status_code=422, detail="local_archive_empty")
        if len(infos) > _LOCAL_ARCHIVE_MAX_MEMBERS:
            raise HTTPException(status_code=422, detail="local_archive_member_count_exceeded")

        members: list[tuple[zipfile.ZipInfo, str]] = []
        seen_paths: dict[str, bool] = {}
        total_size = 0
        has_root_plugin = False
        has_package_descriptor = False
        for info in infos:
            normalized_name = self._normalize_archive_member(info.filename)
            path_key = normalized_name.casefold()
            if path_key in seen_paths:
                raise HTTPException(
                    status_code=422,
                    detail="duplicate_local_archive_member",
                )
            for existing_path, existing_is_dir in seen_paths.items():
                if (
                    path_key.startswith(f"{existing_path}/")
                    and not existing_is_dir
                ) or (
                    existing_path.startswith(f"{path_key}/")
                    and not info.is_dir()
                ):
                    raise HTTPException(
                        status_code=422,
                        detail="conflicting_local_archive_member",
                    )
            seen_paths[path_key] = info.is_dir()

            if info.flag_bits & 0x1:
                raise HTTPException(status_code=422, detail="encrypted_local_archive_member")
            if info.compress_type not in _LOCAL_ARCHIVE_ALLOWED_COMPRESSION:
                raise HTTPException(
                    status_code=422,
                    detail="unsupported_local_archive_compression",
                )
            file_type = (info.external_attr >> 16) & 0o170000
            if file_type not in {0, 0o040000, 0o100000}:
                raise HTTPException(status_code=422, detail="unsafe_local_archive_member")
            if (
                (info.is_dir() and file_type not in {0, 0o040000})
                or (not info.is_dir() and file_type == 0o040000)
            ):
                raise HTTPException(status_code=422, detail="unsafe_local_archive_member")
            if info.file_size < 0 or info.compress_size < 0:
                raise HTTPException(status_code=422, detail="invalid_local_archive_member")
            if not info.is_dir():
                if info.file_size > _LOCAL_ARCHIVE_MAX_MEMBER_BYTES:
                    raise HTTPException(
                        status_code=422,
                        detail="local_archive_member_too_large",
                    )
                total_size += info.file_size
                if total_size > _LOCAL_ARCHIVE_MAX_TOTAL_BYTES:
                    raise HTTPException(
                        status_code=422,
                        detail="local_archive_uncompressed_size_exceeded",
                    )
                ratio = info.file_size / max(1, info.compress_size)
                if ratio > _LOCAL_ARCHIVE_MAX_COMPRESSION_RATIO:
                    raise HTTPException(
                        status_code=422,
                        detail="local_archive_compression_ratio_exceeded",
                    )
                has_root_plugin = has_root_plugin or normalized_name == "plugin.py"
                has_package_descriptor = (
                    has_package_descriptor
                    or normalized_name == _LOCAL_ARCHIVE_DESCRIPTOR_NAME
                )
            members.append((info, normalized_name))

        if not has_root_plugin:
            raise HTTPException(
                status_code=422,
                detail="local_archive_plugin_entrypoint_required",
            )
        if not has_package_descriptor:
            raise HTTPException(
                status_code=422,
                detail="local_archive_descriptor_required",
            )
        return members

    def _normalize_archive_member(self, name: str) -> str:
        raw_name = str(name or "")
        if not raw_name or "\x00" in raw_name:
            raise HTTPException(status_code=422, detail="unsafe_local_archive_member")
        slash_name = raw_name.replace("\\", "/")
        posix_path = PurePosixPath(slash_name)
        windows_path = PureWindowsPath(raw_name)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or ".." in posix_path.parts
            or ".." in windows_path.parts
        ):
            raise HTTPException(status_code=422, detail="unsafe_local_archive_member")
        parts = [part for part in slash_name.split("/") if part not in {"", "."}]
        if not parts:
            raise HTTPException(status_code=422, detail="unsafe_local_archive_member")
        if any(
            not part
            or part == ".."
            or ":" in part
            or part.endswith((" ", "."))
            or any(ord(character) < 32 for character in part)
            or part.split(".", 1)[0].casefold()
            in _LOCAL_ARCHIVE_WINDOWS_RESERVED_NAMES
            for part in parts
        ):
            raise HTTPException(status_code=422, detail="unsafe_local_archive_member")
        return "/".join(parts)

    def _validate_staged_plugin(
        self,
        target_dir: Path,
        item: MarketplaceItem,
    ) -> None:
        if not (target_dir / "plugin.py").is_file():
            raise HTTPException(
                status_code=422,
                detail="local_archive_plugin_entrypoint_required",
            )
        descriptor_path = target_dir / _LOCAL_ARCHIVE_DESCRIPTOR_NAME
        if not descriptor_path.is_file():
            raise HTTPException(status_code=422, detail="local_archive_descriptor_required")
        try:
            descriptor = json.loads(
                descriptor_path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail="invalid_local_archive_descriptor",
            ) from exc
        if not isinstance(descriptor, dict) or set(descriptor) != set(
            _LOCAL_ARCHIVE_DESCRIPTOR_FIELDS
        ):
            raise HTTPException(
                status_code=422,
                detail="invalid_local_archive_descriptor",
            )
        if (
            type(descriptor.get("schema_version")) is not int
            or descriptor["schema_version"] != _LOCAL_ARCHIVE_DESCRIPTOR_SCHEMA_VERSION
        ):
            raise HTTPException(
                status_code=422,
                detail="unsupported_local_archive_descriptor_schema",
            )
        if descriptor.get("name") != item.name or descriptor.get("version") != item.version:
            raise HTTPException(
                status_code=422,
                detail="local_archive_identity_mismatch",
            )
        self._validate_archive_compatibility(descriptor.get("compatibility"), item)
        self._validate_archive_permissions(descriptor.get("permissions"), item)
        self._validate_archive_dependencies(descriptor.get("dependencies"), item)
        if descriptor.get("capability_digest") != item.capability_digest:
            raise HTTPException(
                status_code=422,
                detail="local_archive_capability_digest_mismatch",
            )

    def _validate_archive_compatibility(
        self,
        raw: Any,
        item: MarketplaceItem,
    ) -> None:
        if not isinstance(raw, dict) or set(raw) != {"core_api", "python"}:
            raise HTTPException(
                status_code=422,
                detail="invalid_local_archive_descriptor",
            )
        actual_core = self._descriptor_specifier(raw.get("core_api"), required=True)
        expected_core = self._descriptor_specifier(
            item.compatibility.get("core_api", ""),
            required=True,
        )
        actual_python = self._descriptor_specifier(raw.get("python"), required=False)
        expected_python = self._descriptor_specifier(
            item.compatibility.get("python", ""),
            required=False,
        )
        if actual_core != expected_core or actual_python != expected_python:
            raise HTTPException(
                status_code=422,
                detail="local_archive_compatibility_mismatch",
            )

    @staticmethod
    def _validate_archive_permissions(raw: Any, item: MarketplaceItem) -> None:
        if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
            raise HTTPException(
                status_code=422,
                detail="invalid_local_archive_descriptor",
            )
        permissions = [value.strip() for value in raw]
        if any(not value for value in permissions) or len(set(permissions)) != len(permissions):
            raise HTTPException(
                status_code=422,
                detail="invalid_local_archive_descriptor",
            )
        if sorted(permissions) != sorted(item.permission_ids):
            raise HTTPException(
                status_code=422,
                detail="local_archive_permissions_mismatch",
            )

    def _validate_archive_dependencies(self, raw: Any, item: MarketplaceItem) -> None:
        if not isinstance(raw, list):
            raise HTTPException(
                status_code=422,
                detail="invalid_local_archive_descriptor",
            )
        actual: list[tuple[str, str, bool]] = []
        seen_names: set[str] = set()
        for dependency in raw:
            if not isinstance(dependency, dict) or set(dependency) != {
                "name",
                "version",
                "required",
            }:
                raise HTTPException(
                    status_code=422,
                    detail="invalid_local_archive_descriptor",
                )
            try:
                name = validate_plugin_name(dependency.get("name"))
            except MarketplaceManifestError as exc:
                raise HTTPException(
                    status_code=422,
                    detail="invalid_local_archive_descriptor",
                ) from exc
            if name in seen_names or type(dependency.get("required")) is not bool:
                raise HTTPException(
                    status_code=422,
                    detail="invalid_local_archive_descriptor",
                )
            seen_names.add(name)
            version = self._descriptor_specifier(
                dependency.get("version"),
                required=False,
            )
            actual.append((name, version, dependency["required"]))
        expected = [
            (
                dependency.name,
                self._descriptor_specifier(dependency.version, required=False),
                dependency.required,
            )
            for dependency in item.dependencies
        ]
        if sorted(actual) != sorted(expected):
            raise HTTPException(
                status_code=422,
                detail="local_archive_dependencies_mismatch",
            )

    @staticmethod
    def _descriptor_specifier(raw: Any, *, required: bool) -> str:
        if not isinstance(raw, str):
            raise HTTPException(
                status_code=422,
                detail="invalid_local_archive_descriptor",
            )
        value = raw.strip()
        if not value:
            if required:
                raise HTTPException(
                    status_code=422,
                    detail="invalid_local_archive_descriptor",
                )
            return ""
        try:
            return str(SpecifierSet(normalize_specifier(value)))
        except InvalidSpecifier as exc:
            raise HTTPException(
                status_code=422,
                detail="invalid_local_archive_descriptor",
            ) from exc

    def _require_permission_confirmation(self, item: MarketplaceItem, body: dict[str, Any]) -> None:
        confirmed = {str(value) for value in body.get("confirm_permissions") or []}
        required = set(item.permission_ids)
        if not required.issubset(confirmed):
            raise HTTPException(status_code=403, detail="permission_denied")
        if item.restart_policy != "none" and not bool(body.get("confirm_restart_required")):
            raise HTTPException(status_code=409, detail="plugin_restart_confirmation_required")

    async def _require_dependencies_installed(self, item: MarketplaceItem) -> None:
        await self._require_dependency_contract(item.dependencies)

    async def _require_dependency_contract(
        self,
        dependencies: list[PluginDependency] | tuple[PluginDependency, ...],
    ) -> None:
        for dependency in dependencies:
            if not dependency.required:
                continue
            state = await self.state_store.get(dependency.name)
            if state is None or not state.installed:
                raise HTTPException(status_code=409, detail="plugin_dependency_not_installed")
            if not state.enabled:
                raise HTTPException(status_code=409, detail="plugin_dependency_disabled")
            if dependency.version:
                try:
                    if Version(state.version) not in SpecifierSet(normalize_specifier(dependency.version)):
                        raise HTTPException(status_code=409, detail="plugin_dependency_version_mismatch")
                except InvalidSpecifier as exc:
                    raise HTTPException(status_code=409, detail="invalid_plugin_dependency_specifier") from exc
                except InvalidVersion as exc:
                    raise HTTPException(status_code=409, detail="invalid_plugin_dependency_version") from exc

    async def _reject_installed_dependents(self, plugin_name: str) -> None:
        catalog = self._load_manifest().by_name()
        for state in await self.state_store.list_states():
            if not state.installed:
                continue
            for dependency in self._installed_dependencies(
                state,
                fallback_item=catalog.get(state.plugin_name),
            ):
                if dependency.required and dependency.name == plugin_name:
                    raise HTTPException(status_code=409, detail="plugin_has_dependents")

    async def _reject_enabled_dependents(self, plugin_name: str) -> None:
        catalog = self._load_manifest().by_name()
        for state in await self.state_store.list_states():
            if not state.installed or not state.enabled:
                continue
            for dependency in self._installed_dependencies(
                state,
                fallback_item=catalog.get(state.plugin_name),
            ):
                if dependency.required and dependency.name == plugin_name:
                    raise HTTPException(status_code=409, detail="plugin_has_enabled_dependents")

    async def _reject_incompatible_enabled_dependents(
        self,
        target: MarketplaceItem,
    ) -> None:
        catalog = self._load_manifest().by_name()
        for state in await self.state_store.list_states():
            if not state.installed or not state.enabled:
                continue
            for dependency in self._installed_dependencies(
                state,
                fallback_item=catalog.get(state.plugin_name),
            ):
                if (
                    not dependency.required
                    or dependency.name != target.name
                    or not dependency.version
                ):
                    continue
                try:
                    compatible = Version(target.version) in SpecifierSet(
                        normalize_specifier(dependency.version)
                    )
                except (InvalidSpecifier, InvalidVersion) as exc:
                    raise HTTPException(
                        status_code=409,
                        detail="invalid_plugin_dependency_version",
                    ) from exc
                if not compatible:
                    raise HTTPException(
                        status_code=409,
                        detail="plugin_enabled_dependent_version_mismatch",
                    )

    def _installed_dependencies(
        self,
        state: PluginState,
        *,
        fallback_item: MarketplaceItem | None = None,
    ) -> tuple[PluginDependency, ...]:
        """Read dependency edges from the installed immutable generation."""

        metadata = state.metadata if isinstance(state.metadata, dict) else {}
        manifest = metadata.get("manifest")
        if isinstance(manifest, dict) and "dependencies" in manifest:
            raw_dependencies = manifest.get("dependencies")
            if not isinstance(raw_dependencies, list):
                raise HTTPException(
                    status_code=409,
                    detail="plugin_dependency_contract_invalid",
                )
            parsed: list[PluginDependency] = []
            seen: set[str] = set()
            for raw in raw_dependencies:
                if not isinstance(raw, dict) or set(raw) != {
                    "name",
                    "version",
                    "required",
                }:
                    raise HTTPException(
                        status_code=409,
                        detail="plugin_dependency_contract_invalid",
                    )
                try:
                    name = validate_plugin_name(raw.get("name"))
                    version = self._descriptor_specifier(
                        raw.get("version"),
                        required=False,
                    )
                except (MarketplaceManifestError, HTTPException) as exc:
                    raise HTTPException(
                        status_code=409,
                        detail="plugin_dependency_contract_invalid",
                    ) from exc
                if name in seen or type(raw.get("required")) is not bool:
                    raise HTTPException(
                        status_code=409,
                        detail="plugin_dependency_contract_invalid",
                    )
                seen.add(name)
                parsed.append(
                    PluginDependency(
                        name=name,
                        version=version,
                        required=raw["required"],
                    )
                )
            return tuple(parsed)

        raw_dependencies = metadata.get("dependencies")
        if isinstance(raw_dependencies, list) and all(
            isinstance(raw, str) for raw in raw_dependencies
        ):
            parsed = []
            try:
                for raw in raw_dependencies:
                    dependency = parse_plugin_dependency(raw, owner=state.plugin_name)
                    parsed.append(
                        PluginDependency(
                            name=dependency.name,
                            version=(
                                f">={dependency.minimum_version}"
                                if dependency.minimum_version is not None
                                else ""
                            ),
                            required=True,
                        )
                    )
            except PluginDependencyError as exc:
                raise HTTPException(
                    status_code=409,
                    detail="plugin_dependency_contract_invalid",
                ) from exc
            return tuple(parsed)

        descriptor = self.registry.descriptor(state.plugin_name)
        runtime_dependencies: tuple[str, ...] | None = None
        if descriptor is not None and (
            not state.version or str(descriptor.version) == str(state.version)
        ):
            runtime_dependencies = tuple(descriptor.dependencies)
        else:
            plugin = self.registry.loaded_plugins.get(state.plugin_name)
            if plugin is not None and (
                str(plugin.meta.version) == str(state.version)
                or (state.source == "builtin" and not state.version)
            ):
                runtime_dependencies = tuple(plugin.meta.dependencies)
        if runtime_dependencies is not None:
            parsed = []
            try:
                for raw in runtime_dependencies:
                    dependency = parse_plugin_dependency(raw, owner=state.plugin_name)
                    parsed.append(
                        PluginDependency(
                            name=dependency.name,
                            version=(
                                f">={dependency.minimum_version}"
                                if dependency.minimum_version is not None
                                else ""
                            ),
                            required=True,
                        )
                    )
            except PluginDependencyError as exc:
                raise HTTPException(
                    status_code=409,
                    detail="plugin_dependency_contract_invalid",
                ) from exc
            return tuple(parsed)
        if fallback_item is not None:
            # Marketplace catalogs are mutable discovery inputs, not an
            # installed generation.  Old external rows without an immutable
            # manifest must be repaired/reinstalled instead of silently
            # adopting today's dependency graph.
            raise HTTPException(
                status_code=409,
                detail="plugin_dependency_contract_missing",
            )
        return ()

    def _validate_request_name(self, name: str) -> str:
        try:
            return validate_plugin_name(name)
        except MarketplaceManifestError as exc:
            raise HTTPException(status_code=400, detail="invalid_plugin_name") from exc

    async def _append_event(
        self,
        plugin_name: str,
        event_type: str,
        *,
        status: str = "ok",
        message: str = "",
        metadata: dict[str, Any] | None = None,
        request: Request | None = None,
    ) -> None:
        request_id = ""
        ip_address = ""
        actor_id = ""
        if request is not None:
            request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
            actor_id = request.headers.get("X-Admin-Actor") or request.headers.get("X-Actor-ID") or "admin"
            if request.client is not None:
                ip_address = request.client.host
        await self.state_store.append_event(
            plugin_name,
            event_type,
            status=status,
            actor_id=actor_id,
            request_id=request_id,
            ip_address=ip_address,
            message=message,
            metadata=metadata,
        )


def _canonical_lifecycle_body(body: dict[str, Any]) -> dict[str, Any]:
    canonical = _json_safe(body)
    if not isinstance(canonical, dict):
        return {}
    permissions = canonical.get("confirm_permissions")
    if isinstance(permissions, list):
        canonical["confirm_permissions"] = sorted(
            {str(value) for value in permissions if str(value).strip()}
        )
    return canonical


def _plugin_lifecycle_fingerprint(
    operation: str,
    plugin_name: str,
    body: dict[str, Any],
    *,
    generation_contract: dict[str, Any] | None = None,
) -> str:
    encoded = json.dumps(
        {
            "operation": operation,
            "plugin_name": plugin_name,
            "body": body,
            "generation_contract": generation_contract,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_safe(value: Any) -> Any:
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate json key: {key}")
        result[key] = value
    return result
