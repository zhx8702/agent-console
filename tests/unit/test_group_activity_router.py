from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin.mutation_ledger import (
    MutationIdempotencyConflictError,
    MutationOutcome,
    fingerprint,
)
from plugins.group_activity.router import build_group_activity_router
from plugins.group_activity.store import GroupActivityConfigVersionConflictError


class _FakeStore:
    def __init__(self) -> None:
        self.version = 0

    async def get_config(self, tenant_id: str, session_id: str):
        return {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "enabled": False,
            "idle_minutes": 180,
            "version": self.version,
        }

    async def run_admin_mutation(self, *, identity, audit, mutate):
        _ = (identity, audit)
        change = await mutate()
        return MutationOutcome(
            response=change.response,
            status_code=change.status_code,
            replayed=False,
            mutation_id="mutation-test",
        )

    async def set_config(
        self,
        tenant_id: str,
        session_id: str,
        *,
        expected_version: int,
        **kwargs,
    ):
        before = await self.get_config(tenant_id, session_id)
        if expected_version != self.version:
            raise GroupActivityConfigVersionConflictError(
                expected=expected_version,
                current=self.version,
            )
        self.version += 1
        after = {
            **before,
            **{key: value for key, value in kwargs.items() if value is not None},
            "version": self.version,
        }
        return SimpleNamespace(before=before, after=after)

    async def list_configs(self, tenant_id: str, *, enabled=None, limit: int = 100):
        return [{"tenant_id": tenant_id, "enabled": enabled, "limit": limit}]

    async def list_events(self, tenant_id: str, *, session_id=None, status=None, limit: int = 50):
        return [{"tenant_id": tenant_id, "session_id": session_id, "status": status, "limit": limit}]


class _ReplayStore(_FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.mutations: dict[tuple[str, str], tuple[str, MutationOutcome]] = {}

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
            mutation_id="group-replay-test",
        )
        self.mutations[key] = (request_hash, outcome)
        return outcome


class _Decision:
    def as_dict(self):
        return {"status": "dry_run", "reason": "would_trigger"}


class _FakeService:
    def __init__(self) -> None:
        self.calls = []
        self.scheduler_calls: list[int] = []

    async def process_session(self, tenant_id: str, session_id: str, *, dry_run: bool = True, force: bool = False):
        self.calls.append({"tenant_id": tenant_id, "session_id": session_id, "dry_run": dry_run, "force": force})
        return _Decision()

    async def process_due_sessions(self, *, limit: int = 200):
        self.scheduler_calls.append(limit)
        return {"processed": limit}


def _client(
    service: _FakeService | None = None,
    store: _FakeStore | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_group_activity_router(store or _FakeStore(), service or _FakeService())
    )
    return TestClient(app)


def test_group_activity_router_supports_config_events_and_trigger() -> None:
    service = _FakeService()
    with _client(service) as client:
        config = client.get("/config/demo/room@chatroom")
        updated = client.post(
            "/config/demo/room@chatroom",
            headers={"If-Match": config.headers["etag"]},
            json={
                "enabled": True,
                "idle_minutes": 180,
                "quiet_start": "23:00",
                "quiet_end": "08:00",
                "topic_repeat_window_minutes": 1440,
                "agent_tool_scope": "group_info",
            },
        )
        events = client.get("/events/demo?session_id=room@chatroom&status=completed&limit=5")
        mutation_headers = {"Idempotency-Key": "group-activity-test"}
        trigger = client.post(
            "/trigger/demo/room@chatroom",
            headers=mutation_headers,
            json={"dry_run": False, "force": True},
        )
        run_once = client.post(
            "/scheduler/run-once?limit=3",
            headers=mutation_headers,
        )

    assert config.status_code == 200
    assert config.headers["etag"] == '"0"'
    assert updated.status_code == 200
    assert updated.headers["etag"] == '"1"'
    assert updated.json()["enabled"] is True
    assert updated.json()["quiet_start"] == "23:00"
    assert updated.json()["topic_repeat_window_minutes"] == 1440
    assert updated.json()["agent_tool_scope"] == "group_info"
    assert events.json()["items"][0]["status"] == "completed"
    assert trigger.json()["status"] == "dry_run"
    assert service.calls[0]["dry_run"] is False
    assert service.calls[0]["force"] is True
    assert run_once.json() == {"processed": 3}


def test_group_activity_non_dry_run_requires_idempotency_key() -> None:
    with _client() as client:
        preview = client.post(
            "/trigger/demo/room@chatroom",
            json={"dry_run": True},
        )
        trigger = client.post(
            "/trigger/demo/room@chatroom",
            json={"dry_run": False},
        )
        run_once = client.post("/scheduler/run-once")

    assert preview.status_code == 200
    assert trigger.status_code == 428
    assert run_once.status_code == 428


def test_group_activity_trigger_exactly_replays_and_rejects_key_reuse() -> None:
    service = _FakeService()
    store = _ReplayStore()
    headers = {"Idempotency-Key": "activity-lost-response"}
    with _client(service, store) as client:
        first = client.post(
            "/trigger/demo/room@chatroom",
            headers=headers,
            json={"dry_run": False, "force": False},
        )
        replay = client.post(
            "/trigger/demo/room@chatroom",
            headers=headers,
            json={"dry_run": False, "force": False},
        )
        conflict = client.post(
            "/trigger/demo/room@chatroom",
            headers=headers,
            json={"dry_run": False, "force": True},
        )

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert len(service.calls) == 1
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_key_conflict"


def test_group_activity_scheduler_exactly_replays_and_rejects_key_reuse() -> None:
    service = _FakeService()
    store = _ReplayStore()
    headers = {"Idempotency-Key": "scheduler-lost-response"}
    with _client(service, store) as client:
        first = client.post(
            "/scheduler/run-once?limit=3",
            headers=headers,
        )
        replay = client.post(
            "/scheduler/run-once?limit=3",
            headers=headers,
        )
        conflict = client.post(
            "/scheduler/run-once?limit=4",
            headers=headers,
        )

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json() == {"processed": 3}
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert service.scheduler_calls == [3]
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_key_conflict"


def test_group_activity_router_rejects_idle_under_three_hours() -> None:
    with _client() as client:
        resp = client.post(
            "/config/demo/room@chatroom",
            headers={"If-Match": '"0"'},
            json={"idle_minutes": 120},
        )

    assert resp.status_code == 422


def test_group_activity_router_rejects_invalid_quiet_time_and_repeat_window() -> None:
    with _client() as client:
        bad_time = client.post(
            "/config/demo/room@chatroom",
            headers={"If-Match": '"0"'},
            json={"quiet_start": "25:00"},
        )
        bad_repeat = client.post(
            "/config/demo/room@chatroom",
            headers={"If-Match": '"0"'},
            json={"topic_repeat_window_minutes": 30},
        )
        excessive_daily = client.post(
            "/config/demo/room@chatroom",
            headers={"If-Match": '"0"'},
            json={"max_per_day": 4},
        )

    assert bad_time.status_code == 422
    assert bad_repeat.status_code == 422
    assert excessive_daily.status_code == 422


def test_group_activity_config_requires_if_match_and_returns_current_etag() -> None:
    with _client() as client:
        empty = client.post(
            "/config/demo/room@chatroom",
            headers={"If-Match": '"0"'},
            json={},
        )
        missing = client.post(
            "/config/demo/room@chatroom",
            json={"enabled": True},
        )
        created = client.post(
            "/config/demo/room@chatroom",
            headers={"If-Match": '"0"'},
            json={"enabled": True},
        )
        stale = client.post(
            "/config/demo/room@chatroom",
            headers={"If-Match": '"0"'},
            json={"enabled": False},
        )

    assert empty.status_code == 400
    assert empty.json()["detail"] == "no_mutable_fields"
    assert missing.status_code == 428
    assert created.status_code == 200
    assert created.headers["etag"] == '"1"'
    assert stale.status_code == 409
    assert stale.headers["etag"] == '"1"'
    assert stale.json()["detail"] == {
        "code": "version_conflict",
        "expected_version": 0,
        "current_version": 1,
    }
