from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin.mutation_ledger import MutationOutcome
from plugins.persona_extract.router import build_persona_extract_router
from plugins.persona_extract.store import PersonaApplyJobError


async def _allow_scope(_tenant_id: str, _session_id: str) -> bool:
    return True


class _FakeStore:
    def __init__(self) -> None:
        self.jobs: dict[int, dict] = {}
        self.next_id = 41
        self.update_calls: list[dict[str, object]] = []
        self.request_ids: dict[str, int] = {}

    async def create_job(
        self,
        tenant_id: str,
        session_id: str,
        target_user_id: str,
        target_name: str = "",
        days_limit: int = 90,
        max_messages: int = 2000,
        session_name: str = "",
        connection_id: str = "",
        adapter_id: str = "",
        external_session_id: str = "",
    ) -> int:
        job_id = self.next_id
        self.jobs[job_id] = {
            "id": job_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "session_name": session_name,
            "target_user_id": target_user_id,
            "target_name": target_name,
            "status": "pending",
            "current_stage": "queued",
            "max_messages": max_messages,
            "days_limit": days_limit,
            "checkpoint": {
                "source_identity": {
                    "connection_id": connection_id,
                    "adapter_id": adapter_id,
                    "external_session_id": external_session_id,
                }
            },
            "artifact": None,
            "result_text": "",
            "mode": "",
            "output_slug": "",
        }
        return job_id

    async def create_job_idempotent(self, **kwargs):
        request_id = str(kwargs.pop("request_id"))
        messages = kwargs.pop("messages", None)
        if request_id in self.request_ids:
            return self.jobs[self.request_ids[request_id]], True
        job_id = await self.create_job(**kwargs)
        self.jobs[job_id]["client_request_id"] = request_id
        self.jobs[job_id]["input_messages"] = messages or []
        self.request_ids[request_id] = job_id
        return self.jobs[job_id], False

    async def get_job(self, job_id: int) -> dict | None:
        return self.jobs.get(job_id)

    async def list_jobs(self, tenant_id: str, session_id: str | None = None) -> list[dict]:
        _ = tenant_id
        _ = session_id
        return list(self.jobs.values())

    async def update_job(self, job_id: int, **kwargs) -> None:
        self.update_calls.append({"job_id": job_id, **kwargs})
        self.jobs[job_id].update(kwargs)

    async def requeue_job_idempotent(self, **kwargs):
        job = self.jobs[int(kwargs["job_id"])]
        job.update(status="pending", current_stage="queued", error="")
        return MutationOutcome(
            response=job,
            status_code=202,
            replayed=False,
            mutation_id="fake-requeue",
        )

    async def cancel_job_idempotent(self, **kwargs):
        job = self.jobs[int(kwargs["job_id"])]
        job.update(status="cancelled", current_stage="cancelled", cancel_requested=True)
        return MutationOutcome(
            response=job,
            status_code=200,
            replayed=False,
            mutation_id="fake-cancel",
        )

    async def list_profiles(self, tenant_id: str, session_id: str) -> list[dict]:
        _ = tenant_id
        _ = session_id
        return []

    async def get_profile(self, profile_id: int) -> dict | None:
        _ = profile_id
        return None

    async def upsert_profile(self, **kwargs):
        return kwargs

    async def delete_profile(self, profile_id: int) -> bool:
        _ = profile_id
        return False

    async def upsert_profile_idempotent(self, **kwargs):
        for key in (
            "idempotency_key",
            "actor",
            "actor_kind",
            "roles",
            "trace_id",
            "reason",
        ):
            kwargs.pop(key, None)
        return MutationOutcome(
            response=kwargs,
            status_code=200,
            replayed=False,
            mutation_id="fake-upsert",
        )

    async def apply_job_idempotent(self, **kwargs):
        job = self.jobs.get(int(kwargs["job_id"]))
        if not job:
            raise PersonaApplyJobError("job not found", status_code=404)
        if job["tenant_id"] != kwargs["tenant_id"]:
            raise PersonaApplyJobError("job tenant does not match profile")
        if job["session_id"] != kwargs["session_id"]:
            raise PersonaApplyJobError("job session does not match profile")
        if job["status"] != "completed":
            raise PersonaApplyJobError("job is not completed")
        artifact = job.get("artifact") or {}
        target = artifact.get("target") or {}
        response = {
            "tenant_id": kwargs["tenant_id"],
            "session_id": kwargs["session_id"],
            "channel": kwargs["channel"],
            "source_key": kwargs["source_key"],
            "source_label": kwargs["source_label"],
            "profile_name": kwargs["profile_name"],
            "target_user_id": target.get("user_id") or job.get("target_user_id") or "",
            "target_name": target.get("name") or job.get("target_name") or "",
            "skill_slug": artifact.get("slug") or job.get("output_slug") or "",
            "prompt_text": job.get("result_text") or "",
            "artifact": artifact,
            "enabled": kwargs["enabled"],
            "job_id": kwargs["job_id"],
        }
        return MutationOutcome(
            response=response,
            status_code=200,
            replayed=False,
            mutation_id="fake-apply",
        )

    async def delete_profile_idempotent(self, **kwargs):
        return MutationOutcome(
            response={"deleted": int(kwargs["profile_id"])},
            status_code=200,
            replayed=False,
            mutation_id="fake-delete",
        )


class _FakeScheduler:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store
        self.calls: list[dict[str, object]] = []

    async def schedule_job(self, job_id: int, messages: list[dict] | None = None):
        self.calls.append({"job_id": job_id, "messages": messages})
        await self.store.update_job(job_id, status="pending", current_stage="queued")
        return await self.store.get_job(job_id)


def test_create_job_schedules_background_execution() -> None:
    app = FastAPI()
    store = _FakeStore()
    scheduler = _FakeScheduler(store)
    app.include_router(
        build_persona_extract_router(store, scheduler, scope_gate=_allow_scope)
    )

    with TestClient(app) as client:
        response = client.post(
            "/jobs",
            headers={"Idempotency-Key": "create-41"},
            json={
                "tenant_id": "demo",
                "session_id": "group-1@chatroom",
                "session_name": "测试群",
                "connection_id": "legacy-wechat-default",
                "adapter_id": "wxbot",
                "external_session_id": "group-1@chatroom",
                "target_user_id": "wxid_member_a",
                "target_name": "成员A",
                "days_limit": 30,
                "max_messages": 200,
                "messages": [{"sender_name": "成员A", "text": "你好"}],
            },
        )

    assert response.status_code == 202
    assert response.json()["job_id"] == 41
    assert response.json()["accepted"] is True
    assert response.json()["job"]["current_stage"] == "queued"
    assert response.json()["job"]["checkpoint"]["source_identity"] == {
        "connection_id": "legacy-wechat-default",
        "adapter_id": "wxbot",
        "external_session_id": "group-1@chatroom",
    }
    assert scheduler.calls == [
        {
            "job_id": 41,
            "messages": None,
        }
    ]
    assert store.jobs[41]["input_messages"] == [
        {"sender_name": "成员A", "text": "你好"}
    ]


def test_persona_request_models_reject_unknown_and_oversized_message_fields() -> None:
    app = FastAPI()
    store = _FakeStore()
    scheduler = _FakeScheduler(store)
    app.include_router(
        build_persona_extract_router(store, scheduler, scope_gate=_allow_scope)
    )

    base = {
        "tenant_id": "demo",
        "session_id": "group-1@chatroom",
        "target_user_id": "wxid_member_a",
    }
    with TestClient(app) as client:
        unknown_top = client.post("/jobs", json={**base, "typo": True})
        unknown_message = client.post(
            "/jobs",
            json={
                **base,
                "messages": [{"sender_name": "成员A", "text": "你好", "raw": "secret"}],
            },
        )
        oversized = client.post(
            "/jobs",
            json={**base, "messages": [{"text": "x" * 8_001}]},
        )

    assert unknown_top.status_code == 422
    assert unknown_message.status_code == 422
    assert oversized.status_code == 422


def test_run_job_requeues_failed_job() -> None:
    app = FastAPI()
    store = _FakeStore()
    scheduler = _FakeScheduler(store)
    store.jobs[41] = {
        "id": 41,
        "tenant_id": "demo",
        "session_id": "group-1@chatroom",
        "session_name": "测试群",
        "target_user_id": "wxid_member_a",
        "target_name": "成员A",
        "status": "failed",
        "current_stage": "skill",
        "checkpoint": {"work_md": "ok", "persona_md": "ok"},
        "artifact": None,
        "result_text": "",
        "mode": "",
        "output_slug": "",
    }
    app.include_router(
        build_persona_extract_router(store, scheduler, scope_gate=_allow_scope)
    )

    with TestClient(app) as client:
        response = client.post(
            "/jobs/41/run",
            headers={"Idempotency-Key": "rerun-41"},
            json=[],
        )

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert response.json()["status"] == "pending"
    assert scheduler.calls == [{"job_id": 41, "messages": None}]


def test_cancel_job_is_idempotent_mutation() -> None:
    app = FastAPI()
    store = _FakeStore()
    store.jobs[41] = {
        "id": 41,
        "tenant_id": "demo",
        "session_id": "group-1@chatroom",
        "status": "running",
    }
    app.include_router(build_persona_extract_router(store, scope_gate=_allow_scope))

    with TestClient(app) as client:
        response = client.post(
            "/jobs/41/cancel",
            headers={"Idempotency-Key": "cancel-41"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["cancel_requested"] is True


def test_run_job_scope_gates_opaque_job_before_requeue() -> None:
    app = FastAPI()
    store = _FakeStore()
    scheduler = _FakeScheduler(store)
    store.jobs[41] = {
        "id": 41,
        "tenant_id": "tenant-disabled",
        "session_id": "group-disabled@chatroom",
        "status": "failed",
        "current_stage": "skill",
    }
    gate_calls: list[tuple[str, str]] = []

    async def scope_gate(tenant_id: str, session_id: str) -> bool:
        gate_calls.append((tenant_id, session_id))
        return False

    app.include_router(
        build_persona_extract_router(
            store,
            scheduler,
            scope_gate=scope_gate,
        )
    )

    with TestClient(app) as client:
        response = client.post("/jobs/41/run", json=[])

    assert response.status_code == 503
    assert response.json()["detail"] == "plugin_runtime_disabled"
    assert gate_calls == [("tenant-disabled", "group-disabled@chatroom")]
    assert scheduler.calls == []
    assert store.update_calls == []
    assert store.jobs[41]["status"] == "failed"


def test_run_job_rejects_unknown_message_fields() -> None:
    app = FastAPI()
    store = _FakeStore()
    scheduler = _FakeScheduler(store)
    store.jobs[41] = {
        "id": 41,
        "tenant_id": "demo",
        "session_id": "group-1@chatroom",
        "status": "failed",
    }
    app.include_router(
        build_persona_extract_router(store, scheduler, scope_gate=_allow_scope)
    )

    with TestClient(app) as client:
        response = client.post(
            "/jobs/41/run",
            json=[{"text": "你好", "unsupported": True}],
        )

    assert response.status_code == 422
    assert scheduler.calls == []


def test_apply_job_uses_completed_job_artifact_for_matching_session() -> None:
    app = FastAPI()
    store = _FakeStore()
    store.jobs[41] = {
        "id": 41,
        "tenant_id": "demo",
        "session_id": "group-1@chatroom",
        "session_name": "测试群",
        "target_user_id": "wxid_member_a",
        "target_name": "成员A",
        "status": "completed",
        "current_stage": "done",
        "artifact": {
            "slug": "member-a",
            "target": {"user_id": "wxid_member_a", "name": "成员A"},
            "files": {"skill_prompt": "按成员A的风格回复。"},
        },
        "result_text": "按成员A的风格回复。",
        "mode": "rebuild",
        "output_slug": "member-a",
    }
    app.include_router(
        build_persona_extract_router(store, None, scope_gate=_allow_scope)
    )

    with TestClient(app) as client:
        response = client.post(
            "/profiles/apply-job",
            headers={"Idempotency-Key": "apply-41"},
            json={
                "tenant_id": "demo",
                "session_id": "group-1@chatroom",
                "session_name": "测试群",
                "job_id": 41,
                "channel": "wechat",
                "source_key": "wxbot",
                "profile_name": "成员A",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"] == 41
    assert payload["tenant_id"] == "demo"
    assert payload["session_id"] == "group-1@chatroom"
    assert payload["channel"] == "wechat"
    assert payload["source_key"] == "wxbot"
    assert payload["target_user_id"] == "wxid_member_a"
    assert payload["target_name"] == "成员A"
    assert payload["skill_slug"] == "member-a"
    assert payload["prompt_text"] == "按成员A的风格回复。"


def test_apply_job_rejects_unfinished_or_wrong_session_job() -> None:
    app = FastAPI()
    store = _FakeStore()
    store.jobs[41] = {
        "id": 41,
        "tenant_id": "demo",
        "session_id": "group-2@chatroom",
        "session_name": "其他群",
        "target_user_id": "wxid_member_a",
        "target_name": "成员A",
        "status": "pending",
        "current_stage": "skill",
        "artifact": {"slug": "member-a"},
        "result_text": "按成员A的风格回复。",
        "mode": "rebuild",
        "output_slug": "member-a",
    }
    app.include_router(
        build_persona_extract_router(store, None, scope_gate=_allow_scope)
    )

    with TestClient(app) as client:
        response = client.post(
            "/profiles/apply-job",
            headers={"Idempotency-Key": "apply-wrong-session"},
            json={
                "tenant_id": "demo",
                "session_id": "group-1@chatroom",
                "job_id": 41,
                "channel": "wechat",
                "source_key": "wxbot",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "job session does not match profile"

    store.jobs[41]["session_id"] = "group-1@chatroom"
    with TestClient(app) as client:
        response = client.post(
            "/profiles/apply-job",
            headers={"Idempotency-Key": "apply-unfinished"},
            json={
                "tenant_id": "demo",
                "session_id": "group-1@chatroom",
                "job_id": 41,
                "channel": "wechat",
                "source_key": "wxbot",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "job is not completed"


def test_profile_mutations_require_idempotency_key() -> None:
    app = FastAPI()
    store = _FakeStore()
    store.jobs[41] = {
        "id": 41,
        "tenant_id": "demo",
        "session_id": "group-1@chatroom",
        "status": "completed",
        "artifact": {"slug": "member-a"},
        "result_text": "style",
    }
    app.include_router(
        build_persona_extract_router(store, None, scope_gate=_allow_scope)
    )

    with TestClient(app) as client:
        upsert = client.post(
            "/profiles",
            json={
                "tenant_id": "demo",
                "session_id": "group-1@chatroom",
                "prompt_text": "style",
            },
        )
        apply = client.post(
            "/profiles/apply-job",
            json={
                "tenant_id": "demo",
                "session_id": "group-1@chatroom",
                "job_id": 41,
            },
        )
        delete = client.delete(
            "/profiles/7",
            params={"tenant_id": "demo", "session_id": "group-1@chatroom"},
        )

    for response in (upsert, apply, delete):
        assert response.status_code == 428
        assert response.json()["detail"]["code"] == "idempotency_key_required"
