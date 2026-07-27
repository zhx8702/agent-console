from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.admin.mutation_ledger import (
    MutationIdempotencyConflictError,
    MutationOutcome,
    fingerprint,
)
from plugins.commands.router import build_commands_router
from plugins.commands.store import CommandConfigVersionConflictError
from plugins.moderation.router import build_moderation_router
from plugins.moderation.store import ModerationConfigVersionConflictError


class _CommandService:
    def catalog(self) -> list[dict[str, object]]:
        return [
            {
                "plugin_name": "demo",
                "command": "/hello",
                "aliases": [],
                "admin_only": False,
            }
        ]


class _CommandStore:
    def __init__(self) -> None:
        self.version = 0
        self.config = {
            "tenant_id": "demo",
            "admin_user_ids_text": "",
            "admin_user_ids": [],
            "user_commands_text": "/hello",
            "user_commands": ["/hello"],
            "admin_commands_text": "",
            "admin_commands": [],
            "catalog": _CommandService().catalog(),
            "version": 0,
        }

    async def get_config(self, tenant_id: str, *, catalog):
        return {**self.config, "tenant_id": tenant_id, "catalog": catalog}

    async def set_config(
        self,
        tenant_id: str,
        *,
        expected_version: int,
        catalog,
        **updates,
    ):
        if expected_version != self.version:
            raise CommandConfigVersionConflictError(
                expected=expected_version,
                current=self.version,
            )
        before = await self.get_config(tenant_id, catalog=catalog)
        self.version += 1
        self.config.update(updates)
        self.config["version"] = self.version
        after = await self.get_config(tenant_id, catalog=catalog)
        return SimpleNamespace(before=before, after=after)


class _ModerationStore:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(
            moderation_webhook_allowed_hosts="qyapi.weixin.qq.com"
        )
        self.version = 0
        self.config = {
            "tenant_id": "demo",
            "session_id": "room@chatroom",
            "enabled": False,
            "webhook_url": "",
            "webhook_enabled": False,
            "reminder_mode": "off",
            "reminder_text": "do-not-audit-this-text",
            "version": 0,
        }
        self.keywords: list[dict] = []

    async def run_admin_mutation(self, *, identity, audit, mutate):
        _ = (identity, audit)
        change = await mutate()
        return MutationOutcome(
            response=change.response,
            status_code=change.status_code,
            replayed=False,
            mutation_id="mutation-test",
        )

    async def get_config(self, tenant_id: str, session_id: str):
        return {
            **self.config,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "version": self.version,
        }

    async def set_config(
        self,
        tenant_id: str,
        session_id: str,
        *,
        expected_version: int,
        **updates,
    ):
        self._check(expected_version)
        before = await self.get_config(tenant_id, session_id)
        self.version += 1
        self.config.update(updates)
        after = await self.get_config(tenant_id, session_id)
        return SimpleNamespace(before=before, after=after)

    async def get_keywords_resource(
        self,
        tenant_id: str,
        session_id: str,
        *,
        enabled_only: bool = False,
    ):
        items = [
            item
            for item in self.keywords
            if not enabled_only or item.get("enabled", True)
        ]
        return list(items), self.version

    async def upsert_keywords(
        self,
        tenant_id: str,
        session_id: str,
        entries,
        *,
        replace: bool,
        expected_version: int,
    ):
        self._check(expected_version)
        before = list(self.keywords)
        if replace:
            self.keywords = []
        for entry in entries:
            self.keywords = [
                item for item in self.keywords if item["keyword"] != entry["keyword"]
            ]
            self.keywords.append(
                {
                    "id": len(self.keywords) + 1,
                    "keyword": entry["keyword"],
                    "enabled": entry.get("enabled", True),
                }
            )
        self.version += 1
        return SimpleNamespace(
            before=before,
            after=list(self.keywords),
            version=self.version,
        )

    async def remove_keywords(
        self,
        tenant_id: str,
        session_id: str,
        keywords,
        *,
        expected_version: int,
    ):
        self._check(expected_version)
        before = list(self.keywords)
        if keywords:
            targets = set(keywords)
            self.keywords = [
                item for item in self.keywords if item["keyword"] not in targets
            ]
        else:
            self.keywords = []
        self.version += 1
        return SimpleNamespace(
            before=before,
            after=list(self.keywords),
            version=self.version,
        )

    async def list_sessions(self, tenant_id: str, limit: int = 200):
        return []

    async def get_events(self, *args, **kwargs):
        return []

    def _check(self, expected_version: int) -> None:
        if expected_version != self.version:
            raise ModerationConfigVersionConflictError(
                expected=expected_version,
                current=self.version,
            )


class _ReplayModerationStore(_ModerationStore):
    def __init__(self) -> None:
        super().__init__()
        self.mutations: dict[tuple[str, str], tuple[str, MutationOutcome]] = {}
        self.delete_calls = 0
        self.replace_calls = 0

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
            mutation_id="moderation-replay-test",
        )
        self.mutations[key] = (request_hash, outcome)
        return outcome

    async def remove_keywords(self, *args, **kwargs):
        self.delete_calls += 1
        return await super().remove_keywords(*args, **kwargs)

    async def upsert_keywords(self, *args, **kwargs):
        if kwargs.get("replace"):
            self.replace_calls += 1
        return await super().upsert_keywords(*args, **kwargs)

def _capture_audit_context(app: FastAPI, captured: list[dict]) -> None:
    @app.middleware("http")
    async def capture(request: Request, call_next):
        response = await call_next(request)
        context = getattr(request.state, "admin_audit_context", None)
        if isinstance(context, dict):
            captured.append(dict(context))
        return response


def test_commands_config_etag_precondition_conflict_and_redacted_audit() -> None:
    store = _CommandStore()
    app = FastAPI()
    captured: list[dict] = []
    _capture_audit_context(app, captured)
    app.include_router(build_commands_router(store, _CommandService()))

    with TestClient(app) as client:
        initial = client.get("/config/demo")
        missing = client.post("/config/demo", json={"admin_user_ids_text": "wxid-a"})
        empty = client.post(
            "/config/demo",
            headers={"If-Match": initial.headers["etag"]},
            json={},
        )
        saved = client.post(
            "/config/demo",
            headers={"If-Match": initial.headers["etag"], "X-Trace-ID": "trace-command"},
            json={"admin_user_ids_text": "wxid-a"},
        )
        stale = client.post(
            "/config/demo",
            headers={"If-Match": '"0"'},
            json={"admin_user_ids_text": "wxid-b"},
        )

    assert initial.headers["etag"] == '"0"'
    assert initial.headers["cache-control"] == "no-store"
    assert missing.status_code == 428
    assert empty.status_code == 400
    assert empty.json()["detail"] == "no_mutable_fields"
    assert saved.status_code == 200
    assert saved.headers["etag"] == '"1"'
    assert stale.status_code == 409
    assert stale.headers["etag"] == '"1"'
    assert captured[-1]["policy_version"] == 1
    assert captured[-1]["trace_id"] == "trace-command"
    assert "admin_user_ids_text" not in captured[-1]["after_state"]


def test_moderation_config_and_keywords_share_atomic_version_contract() -> None:
    store = _ModerationStore()
    app = FastAPI()
    captured: list[dict] = []
    _capture_audit_context(app, captured)
    app.include_router(build_moderation_router(store))
    config_path = "/config/demo/room@chatroom"
    keyword_path = "/keywords/demo/room@chatroom"

    with TestClient(app) as client:
        initial = client.get(config_path)
        missing = client.post(config_path, json={"enabled": True})
        saved = client.post(
            config_path,
            headers={"If-Match": initial.headers["etag"], "X-Trace-ID": "trace-mod"},
            json={"enabled": True, "reminder_text": "private reminder body"},
        )
        keyword_initial = client.get(keyword_path)
        keyword_missing = client.post(keyword_path, json={"keyword": "secret"})
        keyword_saved = client.post(
            keyword_path,
            headers={"If-Match": keyword_initial.headers["etag"]},
            json={"keyword": "secret"},
        )
        destructive_replace_missing_key = client.post(
            keyword_path,
            headers={"If-Match": '"2"'},
            json={"keywords": ["secret"], "replace": True},
        )
        untargeted_delete = client.request(
            "DELETE",
            keyword_path,
            headers={"If-Match": '"2"'},
        )
        targeted_delete_missing_key = client.request(
            "DELETE",
            keyword_path,
            headers={"If-Match": '"2"'},
            params={"keyword": "secret"},
        )
        stale_delete = client.request(
            "DELETE",
            keyword_path,
            headers={
                "If-Match": '"1"',
                "Idempotency-Key": "moderation-stale-delete",
            },
            json={"clear_all": True},
        )

    assert initial.headers["etag"] == '"0"'
    assert missing.status_code == 428
    assert saved.headers["etag"] == '"1"'
    assert keyword_initial.headers["etag"] == '"1"'
    assert keyword_missing.status_code == 428
    assert keyword_saved.headers["etag"] == '"2"'
    assert destructive_replace_missing_key.status_code == 428
    assert untargeted_delete.status_code == 400
    assert targeted_delete_missing_key.status_code == 428
    assert stale_delete.status_code == 409
    assert stale_delete.headers["etag"] == '"2"'
    config_audit = next(
        item for item in captured if item["target_type"] == "plugin_moderation_config"
    )
    assert config_audit["policy_version"] == 1
    assert config_audit["trace_id"] == "trace-mod"
    assert "webhook_url" not in config_audit["before_state"]
    assert "reminder_text" not in config_audit["after_state"]
    keyword_audit = next(
        item for item in captured if item["target_type"] == "plugin_moderation_keywords"
    )
    assert keyword_audit["after_state"] == {
        "version": 2,
        "keyword_count": 1,
        "enabled_keyword_count": 1,
    }


def test_moderation_keyword_delete_exactly_replays_and_rejects_key_reuse() -> None:
    store = _ReplayModerationStore()
    store.keywords = [
        {"id": 1, "keyword": "alpha", "enabled": True},
        {"id": 2, "keyword": "beta", "enabled": True},
    ]
    app = FastAPI()
    app.include_router(build_moderation_router(store))
    path = "/keywords/demo/room@chatroom"
    headers = {
        "If-Match": '"0"',
        "Idempotency-Key": "moderation-delete-lost-response",
    }

    with TestClient(app) as client:
        first = client.request(
            "DELETE",
            path,
            headers=headers,
            json={"keyword": "alpha"},
        )
        replay = client.request(
            "DELETE",
            path,
            headers=headers,
            json={"keyword": "alpha"},
        )
        conflict = client.request(
            "DELETE",
            path,
            headers=headers,
            json={"keyword": "beta"},
        )

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.headers["etag"] == replay.headers["etag"] == '"1"'
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert store.delete_calls == 1
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_key_conflict"


def test_moderation_keyword_replace_exactly_replays_and_rejects_key_reuse() -> None:
    store = _ReplayModerationStore()
    store.keywords = [{"id": 1, "keyword": "old", "enabled": True}]
    app = FastAPI()
    app.include_router(build_moderation_router(store))
    path = "/keywords/demo/room@chatroom"
    headers = {
        "If-Match": '"0"',
        "Idempotency-Key": "moderation-replace-lost-response",
    }

    with TestClient(app) as client:
        first = client.post(
            path,
            headers=headers,
            json={"keywords": ["alpha"], "replace": True},
        )
        replay = client.post(
            path,
            headers=headers,
            json={"keywords": ["alpha"], "replace": True},
        )
        conflict = client.post(
            path,
            headers=headers,
            json={"keywords": ["beta"], "replace": True},
        )

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.headers["etag"] == replay.headers["etag"] == '"1"'
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert store.replace_calls == 1
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_key_conflict"
