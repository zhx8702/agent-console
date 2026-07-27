from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin.authorization import AdminRole, Principal
from app.admin.mutation_ledger import (
    MutationIdempotencyConflictError,
    MutationOutcome,
    fingerprint,
)
from plugins.repeater import router as repeater_router
from plugins.repeater.router import build_repeater_router
from plugins.repeater.store import (
    RepeaterConfigMutation,
    RepeaterConfigVersionConflictError,
)


class _FakeStore:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(
            admin_bearer_token="token",
            admin_allow_bearer_fallback=True,
            admin_session_cookie_name="agent_console_admin",
            wxbot_sdk_url="http://127.0.0.1:5080",
            wxbot_api_token="sdk-secret",
        )
        self.config = {
            "tenant_id": "demo",
            "session_id": "group-1@chatroom",
            "enabled": False,
            "cooldown_seconds": 300,
            "version": 1,
            "updated_at": None,
        }

    async def run_admin_mutation(self, *, identity, audit, mutate):
        _ = identity, audit
        change = await mutate()
        return MutationOutcome(
            response=change.response,
            status_code=change.status_code,
            replayed=False,
            mutation_id="repeater-test-mutation",
        )

    async def get_config(self, tenant_id: str, session_id: str):
        return {**self.config, "tenant_id": tenant_id, "session_id": session_id}

    async def set_config(
        self,
        tenant_id: str,
        session_id: str,
        *,
        expected_version: int,
        enabled=None,
        cooldown_seconds=None,
    ):
        before = await self.get_config(tenant_id, session_id)
        if expected_version != self.config["version"]:
            raise RepeaterConfigVersionConflictError(
                expected=expected_version,
                current=self.config["version"],
            )
        if enabled is not None:
            self.config["enabled"] = enabled
        if cooldown_seconds is not None:
            self.config["cooldown_seconds"] = cooldown_seconds
        self.config["version"] += 1
        after = {**self.config, "tenant_id": tenant_id, "session_id": session_id}
        return RepeaterConfigMutation(before=before, after=after)

    async def list_events(self, tenant_id: str, session_id: str | None = None, limit: int = 50):
        return [
            {
                "id": 1,
                "tenant_id": tenant_id,
                "session_id": session_id or "group-1@chatroom",
                "content_text": "复读",
            }
        ]


class _ReplayStore(_FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.mutations: dict[tuple[str, str], tuple[str, MutationOutcome]] = {}
        self.set_calls = 0

    async def run_admin_mutation(self, *, identity, audit, mutate):
        _ = audit
        key = (identity.operation, identity.idempotency_key)
        request_hash = fingerprint(
            {
                "resource_key": identity.resource_key,
                "request_payload": identity.request_payload,
            }
        )
        existing = self.mutations.get(key)
        if existing is not None:
            if existing[0] != request_hash:
                raise MutationIdempotencyConflictError("key reused")
            previous = existing[1]
            return MutationOutcome(
                response=previous.response,
                status_code=previous.status_code,
                replayed=True,
                mutation_id=previous.mutation_id,
            )
        change = await mutate()
        outcome = MutationOutcome(
            response=change.response,
            status_code=change.status_code,
            replayed=False,
            mutation_id="repeater-replay-test",
        )
        self.mutations[key] = (request_hash, outcome)
        return outcome

    async def set_config(self, *args, **kwargs):
        self.set_calls += 1
        return await super().set_config(*args, **kwargs)


def test_repeater_router_supports_config_and_events() -> None:
    app = FastAPI()
    app.include_router(build_repeater_router(_FakeStore()))

    with TestClient(app) as client:
        get_resp = client.get(
            "/config/demo/group-1@chatroom",
            headers={"Authorization": "Bearer token"},
        )
        set_resp = client.post(
            "/config/demo/group-1@chatroom",
            headers={
                "Authorization": "Bearer token",
                "If-Match": get_resp.headers["etag"],
                "Idempotency-Key": "repeater-save",
            },
            json={"enabled": True, "cooldown_seconds": 120},
        )
        events_resp = client.get(
            "/events/demo?session_id=group-1@chatroom&limit=10",
            headers={"Authorization": "Bearer token"},
        )

    assert get_resp.status_code == 200
    assert get_resp.json()["enabled"] is False
    assert get_resp.headers["etag"] == '"1"'
    assert get_resp.headers["cache-control"] == "no-store"
    assert set_resp.status_code == 200
    assert set_resp.json()["enabled"] is True
    assert set_resp.json()["cooldown_seconds"] == 120
    assert set_resp.json()["version"] == 2
    assert set_resp.headers["etag"] == '"2"'
    assert events_resp.status_code == 200
    assert events_resp.json()["items"][0]["content_text"] == "复读"


def test_repeater_router_requires_if_match_and_rejects_stale_writes() -> None:
    store = _FakeStore()
    app = FastAPI()
    app.include_router(build_repeater_router(store))

    with TestClient(app) as client:
        missing = client.post(
            "/config/demo/group-1@chatroom",
            headers={"Authorization": "Bearer token"},
            json={"enabled": True},
        )
        first = client.post(
            "/config/demo/group-1@chatroom",
            headers={
                "Authorization": "Bearer token",
                "If-Match": '"1"',
                "Idempotency-Key": "repeater-first",
            },
            json={"enabled": True},
        )
        stale = client.post(
            "/config/demo/group-1@chatroom",
            headers={
                "Authorization": "Bearer token",
                "If-Match": '"1"',
                "Idempotency-Key": "repeater-stale",
            },
            json={"cooldown_seconds": 120},
        )

    assert missing.status_code == 428
    assert first.status_code == 200
    assert stale.status_code == 409
    assert stale.headers["etag"] == '"2"'
    assert stale.json()["detail"] == {
        "code": "version_conflict",
        "expected_version": 1,
        "current_version": 2,
    }


def test_repeater_config_requires_key_replays_and_rejects_key_reuse() -> None:
    store = _ReplayStore()
    app = FastAPI()
    app.include_router(build_repeater_router(store))
    path = "/config/demo/group-1@chatroom"
    base_headers = {
        "Authorization": "Bearer token",
        "If-Match": '"1"',
    }
    headers = {**base_headers, "Idempotency-Key": "repeater-lost-response"}

    with TestClient(app) as client:
        missing = client.post(path, headers=base_headers, json={"enabled": True})
        first = client.post(path, headers=headers, json={"enabled": True})
        replay = client.post(path, headers=headers, json={"enabled": True})
        conflict = client.post(
            path,
            headers=headers,
            json={"cooldown_seconds": 120},
        )

    assert missing.status_code == 428
    assert missing.json()["detail"] == {"code": "idempotency_key_required"}
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.headers["etag"] == replay.headers["etag"] == '"2"'
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {"code": "idempotency_key_conflict"}
    assert store.set_calls == 1


@pytest.mark.asyncio
async def test_repeater_concurrent_writers_only_one_wins() -> None:
    class _LockedStore(_FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.lock = asyncio.Lock()

        async def set_config(self, *args, **kwargs):
            async with self.lock:
                await asyncio.sleep(0)
                return await super().set_config(*args, **kwargs)

    store = _LockedStore()
    app = FastAPI()
    app.include_router(build_repeater_router(store))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        responses = await asyncio.gather(
            client.post(
                "/config/demo/group-1@chatroom",
                headers={
                    "Authorization": "Bearer token",
                    "If-Match": '"1"',
                    "Idempotency-Key": "repeater-concurrent-a",
                },
                json={"cooldown_seconds": 120},
            ),
            client.post(
                "/config/demo/group-1@chatroom",
                headers={
                    "Authorization": "Bearer token",
                    "If-Match": '"1"',
                    "Idempotency-Key": "repeater-concurrent-b",
                },
                json={"cooldown_seconds": 180},
            ),
        )

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert store.config["version"] == 2


def test_repeater_direct_config_never_mutates_sdk_gate() -> None:
    store = _FakeStore()
    app = FastAPI()
    app.include_router(build_repeater_router(store))

    with TestClient(app) as client:
        response = client.post(
            "/config/demo/group-1@chatroom",
            headers={
                "Authorization": "Bearer token",
                "If-Match": '"1"',
                "Idempotency-Key": "repeater-direct-config",
            },
            json={"enabled": True},
        )

    assert response.status_code == 200
    assert "sdk_capture_adjustment" not in response.json()
    assert "sdk-secret" not in response.text


def test_repeater_scope_fails_closed() -> None:
    app = FastAPI()
    app.include_router(build_repeater_router(_FakeStore()))

    with TestClient(app) as client:
        unauthenticated = client.get("/config/demo/group-1@chatroom")
        private_session = client.get(
            "/config/demo/private-user",
            headers={"Authorization": "Bearer token"},
        )

    assert unauthenticated.status_code == 401
    assert private_session.status_code == 400


def test_repeater_group_scoped_principal_cannot_cross_group_or_list_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = Principal(
        subject="group-operator",
        roles=(AdminRole.GROUP_OPERATOR.value,),
        tenant_ids=("demo",),
        group_ids=("demo:group-1@chatroom",),
        auth_kind="test",
    )
    monkeypatch.setattr(
        repeater_router,
        "authenticate_admin_request",
        lambda _request, _settings: principal,
    )
    app = FastAPI()
    app.include_router(build_repeater_router(_FakeStore()))

    with TestClient(app) as client:
        allowed = client.get("/config/demo/group-1@chatroom")
        crossed = client.get("/config/demo/group-2@chatroom")
        tenant_collection = client.get("/events/demo")

    assert allowed.status_code == 200
    assert crossed.status_code == 403
    assert tenant_collection.status_code == 400
