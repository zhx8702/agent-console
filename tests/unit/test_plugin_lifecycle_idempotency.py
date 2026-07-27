from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import pytest
from fastapi import HTTPException

from app.admin.authorization import AdminPermission
from app.admin.route_permissions import DEFAULT_ROUTE_PERMISSION_REGISTRY
from app.plugin.manager import PluginManager, _settle_under_cancellation
from app.plugin.state import (
    PLUGIN_LIFECYCLE_COMPLETED,
    PLUGIN_LIFECYCLE_IN_PROGRESS,
    PluginLifecycleClaim,
    PluginLifecycleOperation,
)


class _LifecycleStoreDouble:
    def __init__(self) -> None:
        self.records: dict[str, PluginLifecycleOperation] = {}
        self._lock = asyncio.Lock()
        self.fail_complete_once = False
        self.renew_calls = 0
        self.fence_depth = 0
        self._execution_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifecycle_execution_fence(
        self,
        *,
        idempotency_key_hash: str,
        claim_token: str,
        lease_seconds: int,
    ):
        _ = lease_seconds
        async with self._execution_lock:
            current = self.records[idempotency_key_hash]
            if current.claim_token != claim_token:
                raise RuntimeError("plugin_lifecycle_claim_lost")
            self.fence_depth += 1
            try:
                yield
            finally:
                self.fence_depth -= 1

    async def claim_lifecycle_operation(
        self,
        *,
        idempotency_key_hash: str,
        request_fingerprint: str,
        operation: str,
        plugin_name: str,
        claim_token: str,
        lease_seconds: int,
    ) -> PluginLifecycleClaim:
        _ = lease_seconds
        async with self._lock:
            existing = self.records.get(idempotency_key_hash)
            if existing is not None:
                if (
                    existing.request_fingerprint != request_fingerprint
                    or existing.operation != operation
                    or existing.plugin_name != plugin_name
                ):
                    return PluginLifecycleClaim(operation=existing)
                if existing.status == PLUGIN_LIFECYCLE_COMPLETED:
                    return PluginLifecycleClaim(operation=existing)
                if existing.claim_token:
                    return PluginLifecycleClaim(operation=existing)
                reclaimed = replace(
                    existing,
                    claim_token=claim_token,
                    attempt_count=existing.attempt_count + 1,
                )
                self.records[idempotency_key_hash] = reclaimed
                return PluginLifecycleClaim(operation=reclaimed, claimed=True)

            busy = next(
                (
                    record
                    for record in self.records.values()
                    if record.status == PLUGIN_LIFECYCLE_IN_PROGRESS
                ),
                None,
            )
            if busy is not None:
                return PluginLifecycleClaim(operation=busy, plugin_busy=True)
            created = PluginLifecycleOperation(
                idempotency_key_hash=idempotency_key_hash,
                request_fingerprint=request_fingerprint,
                operation=operation,
                plugin_name=plugin_name,
                status=PLUGIN_LIFECYCLE_IN_PROGRESS,
                claim_token=claim_token,
                attempt_count=1,
                result=None,
                before_state=None,
                after_state=None,
                policy_version=0,
            )
            self.records[idempotency_key_hash] = created
            return PluginLifecycleClaim(operation=created, claimed=True)

    async def record_lifecycle_before_state(
        self,
        *,
        idempotency_key_hash: str,
        claim_token: str,
        before_state: dict[str, Any],
    ) -> PluginLifecycleOperation:
        async with self._lock:
            current = self.records[idempotency_key_hash]
            assert current.claim_token == claim_token
            updated = replace(
                current,
                before_state=current.before_state or dict(before_state),
            )
            self.records[idempotency_key_hash] = updated
            return updated

    async def complete_lifecycle_operation(
        self,
        *,
        idempotency_key_hash: str,
        claim_token: str,
        result: dict[str, Any],
        after_state: dict[str, Any],
    ) -> PluginLifecycleOperation:
        async with self._lock:
            current = self.records[idempotency_key_hash]
            assert current.claim_token == claim_token
            if self.fail_complete_once:
                self.fail_complete_once = False
                raise RuntimeError("injected_completion_failure")
            completed = replace(
                current,
                status=PLUGIN_LIFECYCLE_COMPLETED,
                claim_token="",
                result=dict(result),
                after_state=dict(after_state),
                policy_version=1,
            )
            self.records[idempotency_key_hash] = completed
            return completed

    async def renew_lifecycle_claim(
        self,
        *,
        idempotency_key_hash: str,
        claim_token: str,
        lease_seconds: int,
    ) -> bool:
        _ = lease_seconds
        async with self._lock:
            self.renew_calls += 1
            current = self.records[idempotency_key_hash]
            return (
                current.status == PLUGIN_LIFECYCLE_IN_PROGRESS
                and current.claim_token == claim_token
            )

    async def release_lifecycle_claim(
        self,
        *,
        idempotency_key_hash: str,
        claim_token: str,
        error_code: str,
    ) -> None:
        _ = error_code
        async with self._lock:
            current = self.records[idempotency_key_hash]
            if current.claim_token == claim_token:
                self.records[idempotency_key_hash] = replace(current, claim_token="")

    async def get_lifecycle_operation(
        self,
        idempotency_key_hash: str,
    ) -> PluginLifecycleOperation | None:
        return self.records.get(idempotency_key_hash)


class _LifecycleManagerHarness(PluginManager):
    def __init__(self, state_store: _LifecycleStoreDouble) -> None:
        self.state_store = state_store  # type: ignore[assignment]
        self.side_effect_count = 0
        self.enabled = False
        self.action_delay = 0.01
        self.catalog_generation: dict[str, Any] | None = None
        self.dynamic_mutations_allowed = True

    def _require_dynamic_mutation_allowed(self) -> None:
        if not self.dynamic_mutations_allowed:
            raise HTTPException(
                status_code=403,
                detail="dynamic_plugin_mutations_disabled",
            )

    def _lifecycle_marketplace_item(
        self,
        operation: str,
        plugin_name: str,
        body: dict[str, Any],
    ) -> Any:
        _ = operation, plugin_name, body
        if self.catalog_generation is None:
            return None

        class _Item:
            name = "draw"

            def __init__(self, generation: dict[str, Any]) -> None:
                self._generation = generation

            def as_manifest_dict(self) -> dict[str, Any]:
                return dict(self._generation)

        return _Item(self.catalog_generation)

    async def _lifecycle_state_snapshot(self, plugin_name: str) -> dict[str, Any]:
        return {
            "plugin_name": plugin_name,
            "exists": True,
            "installed": True,
            "enabled": self.enabled,
            "runtime_active": self.enabled,
            "runtime_initialized": True,
        }

    async def _run_lifecycle_action(
        self,
        operation: str,
        plugin_name: str,
        body: dict[str, Any],
        request: Any,
    ) -> dict[str, Any]:
        _ = request
        assert self.state_store.fence_depth == 1
        self.side_effect_count += 1
        await asyncio.sleep(self.action_delay)
        if body.get("fail"):
            raise HTTPException(status_code=422, detail="injected_validation_failure")
        self.enabled = operation == "enable"
        return {
            "plugin": {"name": plugin_name, "enabled": self.enabled},
            "operation": operation,
        }

    async def _recover_lifecycle_response(
        self,
        operation: str,
        plugin_name: str,
        body: dict[str, Any],
        before_state: dict[str, Any],
        *,
        marketplace_item: Any = None,
    ) -> dict[str, Any] | None:
        _ = body, before_state, marketplace_item
        assert self.state_store.fence_depth == 1
        if (operation == "enable" and self.enabled) or (
            operation == "disable" and not self.enabled
        ):
            return {
                "plugin": {"name": plugin_name, "enabled": self.enabled},
                "operation": operation,
            }
        return None


@pytest.mark.asyncio
async def test_lifecycle_concurrent_same_key_runs_side_effect_once_and_replays_exactly() -> None:
    manager = _LifecycleManagerHarness(_LifecycleStoreDouble())

    first, second = await asyncio.gather(
        manager.execute_lifecycle(
            "enable",
            "draw",
            {},
            None,
            idempotency_key="plugin-enable-draw-1",
        ),
        manager.execute_lifecycle(
            "enable",
            "draw",
            {},
            None,
            idempotency_key="plugin-enable-draw-1",
        ),
    )

    assert first.response == second.response
    assert sorted([first.idempotent_replayed, second.idempotent_replayed]) == [False, True]
    assert manager.side_effect_count == 1


@pytest.mark.asyncio
async def test_lifecycle_disabled_control_plane_rejects_before_durable_claim() -> None:
    store = _LifecycleStoreDouble()
    manager = _LifecycleManagerHarness(store)
    manager.dynamic_mutations_allowed = False

    with pytest.raises(HTTPException) as raised:
        await manager.execute_lifecycle(
            "enable",
            "draw",
            {},
            None,
            idempotency_key="plugin-enable-disabled-control-plane",
        )

    assert raised.value.status_code == 403
    assert raised.value.detail == "dynamic_plugin_mutations_disabled"
    assert store.records == {}
    assert manager.side_effect_count == 0


@pytest.mark.asyncio
async def test_lifecycle_serializes_different_plugin_graph_mutations() -> None:
    manager = _LifecycleManagerHarness(_LifecycleStoreDouble())
    manager.action_delay = 0.08

    first = asyncio.create_task(
        manager.execute_lifecycle(
            "enable",
            "draw",
            {},
            None,
            idempotency_key="plugin-enable-draw-global-lock",
        )
    )
    while manager.side_effect_count == 0:
        await asyncio.sleep(0)

    with pytest.raises(HTTPException) as raised:
        await manager.execute_lifecycle(
            "disable",
            "memory",
            {},
            None,
            idempotency_key="plugin-disable-memory-global-lock",
        )

    await first
    assert raised.value.status_code == 409
    assert raised.value.detail == "plugin_lifecycle_operation_in_progress"
    assert manager.side_effect_count == 1


@pytest.mark.asyncio
async def test_settlement_finishes_before_repeated_cancellation_escapes() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def operation() -> str:
        started.set()
        await release.wait()
        finished.set()
        return "settled"

    task = asyncio.create_task(
        _settle_under_cancellation(operation(), label="unit_test_settlement")
    )
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)

    assert not task.done()
    assert not finished.is_set()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished.is_set()


@pytest.mark.asyncio
async def test_lifecycle_same_key_different_payload_conflicts() -> None:
    manager = _LifecycleManagerHarness(_LifecycleStoreDouble())
    await manager.execute_lifecycle(
        "install",
        "draw",
        {"confirm_permissions": ["admin_api"]},
        None,
        idempotency_key="plugin-install-draw-1",
    )

    with pytest.raises(HTTPException) as raised:
        await manager.execute_lifecycle(
            "install",
            "draw",
            {"confirm_permissions": ["admin_api", "network"]},
            None,
            idempotency_key="plugin-install-draw-1",
        )

    assert raised.value.status_code == 409
    assert raised.value.detail == "plugin_lifecycle_idempotency_conflict"
    assert manager.side_effect_count == 1


@pytest.mark.asyncio
async def test_lifecycle_recovers_after_effect_when_durable_response_write_fails() -> None:
    store = _LifecycleStoreDouble()
    store.fail_complete_once = True
    manager = _LifecycleManagerHarness(store)

    with pytest.raises(RuntimeError, match="injected_completion_failure"):
        await manager.execute_lifecycle(
            "enable",
            "draw",
            {},
            None,
            idempotency_key="plugin-enable-draw-1",
        )

    recovered = await manager.execute_lifecycle(
        "enable",
        "draw",
        {},
        None,
        idempotency_key="plugin-enable-draw-1",
    )
    replayed = await manager.execute_lifecycle(
        "enable",
        "draw",
        {},
        None,
        idempotency_key="plugin-enable-draw-1",
    )

    assert recovered.response == replayed.response
    assert replayed.idempotent_replayed is True
    assert manager.side_effect_count == 1


@pytest.mark.asyncio
async def test_lifecycle_same_key_cannot_cross_catalog_generation() -> None:
    store = _LifecycleStoreDouble()
    store.fail_complete_once = True
    manager = _LifecycleManagerHarness(store)
    manager.catalog_generation = {
        "name": "draw",
        "version": "1.0.0",
        "package": {
            "type": "local_archive",
            "uri": "draw-v1.zip",
            "checksum": "sha256:v1",
        },
    }

    with pytest.raises(RuntimeError, match="injected_completion_failure"):
        await manager.execute_lifecycle(
            "upgrade",
            "draw",
            {},
            None,
            idempotency_key="plugin-upgrade-draw-generation",
        )

    manager.catalog_generation = {
        "name": "draw",
        "version": "1.0.0",
        "package": {
            "type": "local_archive",
            "uri": "draw-v1-repacked.zip",
            "checksum": "sha256:changed",
        },
    }
    with pytest.raises(HTTPException) as raised:
        await manager.execute_lifecycle(
            "upgrade",
            "draw",
            {},
            None,
            idempotency_key="plugin-upgrade-draw-generation",
        )

    assert raised.value.status_code == 409
    assert raised.value.detail == "plugin_lifecycle_idempotency_conflict"
    assert manager.side_effect_count == 1


@pytest.mark.asyncio
async def test_lifecycle_replays_deterministic_http_error_without_second_effect() -> None:
    manager = _LifecycleManagerHarness(_LifecycleStoreDouble())

    with pytest.raises(HTTPException) as first:
        await manager.execute_lifecycle(
            "enable",
            "draw",
            {"fail": True},
            None,
            idempotency_key="plugin-enable-draw-failure-1",
        )
    with pytest.raises(HTTPException) as replay:
        await manager.execute_lifecycle(
            "enable",
            "draw",
            {"fail": True},
            None,
            idempotency_key="plugin-enable-draw-failure-1",
        )

    assert first.value.status_code == replay.value.status_code == 422
    assert first.value.detail == replay.value.detail == "injected_validation_failure"
    assert replay.value.headers == {"Idempotent-Replayed": "true"}
    assert manager.side_effect_count == 1


@pytest.mark.asyncio
async def test_lifecycle_renews_claim_while_action_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.plugin.manager._PLUGIN_LIFECYCLE_LEASE_SECONDS", 0.03)
    store = _LifecycleStoreDouble()
    manager = _LifecycleManagerHarness(store)
    manager.action_delay = 0.08

    result = await manager.execute_lifecycle(
        "enable",
        "draw",
        {},
        None,
        idempotency_key="plugin-enable-draw-renewal",
    )

    assert result.response["operation"] == "enable"
    assert store.renew_calls >= 1


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/admin/plugins/install"),
        ("POST", "/v1/admin/plugins/{name}/enable"),
        ("POST", "/v1/admin/plugins/{name}/disable"),
        ("POST", "/v1/admin/plugins/{name}/upgrade"),
        ("POST", "/v1/admin/plugins/{name}/uninstall"),
        ("POST", "/v1/admin/dlq/messages/{entry_id}/replay"),
        ("DELETE", "/v1/admin/dlq/messages/{entry_id}"),
    ],
)
def test_high_risk_lifecycle_and_dlq_routes_require_danger_permission(
    method: str,
    path: str,
) -> None:
    declaration = DEFAULT_ROUTE_PERMISSION_REGISTRY.get(method, path)
    assert declaration is not None
    assert declaration.permission is AdminPermission.DANGER
