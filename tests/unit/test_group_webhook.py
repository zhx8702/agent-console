from __future__ import annotations

from importlib import import_module
from io import StringIO
from types import SimpleNamespace
from typing import Any

import orjson
import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.channel import ChannelSendOptions, ChannelSendResult, ChannelTarget
from app.channel.identity import LEGACY_WXBOT_CONNECTION_ID, canonical_conversation_id
from app.infra.runtime_schema import (
    RUNTIME_SCHEMA_INDEX_CONTRACTS,
    RUNTIME_SCHEMA_REVISION,
    RUNTIME_SCHEMA_TABLES,
)
from plugins.wxbot.group_webhook import (
    GroupWebhookRecord,
    get_group_webhook_by_token,
    hash_webhook_token,
    new_webhook_token,
    normalize_group_session_ids,
    public_webhook_document,
    send_group_webhook_text,
    token_hint,
    webhook_public_url,
)
from plugins.wxbot.group_webhook_router import build_group_webhook_router
from plugins.wxbot.router import build_wxbot_router
from plugins.wxbot.store import WxbotStore
from tests.unit._fakes import InMemoryRedis
from tests.unit.test_wxbot_router import _FakeBridge, _FakeStore

_MODULE = "migrations.versions.20260910_0052_wxbot_group_webhooks"


def _record(**overrides: object) -> GroupWebhookRecord:
    payload: dict[str, Any] = {
        "webhook_id": "gwh_test",
        "tenant_id": "default",
        "connection_id": LEGACY_WXBOT_CONNECTION_ID,
        "external_session_id": "room@chatroom",
        "canonical_session_id": "room@chatroom",
        "session_name": "测试群",
        "token_hash": "abc",
        "token_hint": "wxyz",
        "enabled": True,
        "created_by": "tester",
        "created_at": None,
        "last_used_at": None,
        "revoked_at": None,
    }
    payload.update(overrides)
    return GroupWebhookRecord(**payload)


def _render(monkeypatch, operation: str) -> tuple[object, str]:
    migration = import_module(_MODULE)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, operation)()
    return migration, output.getvalue()


def test_group_webhook_upgrade_is_runtime_head(monkeypatch) -> None:
    migration, rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0052_wxbot_group_webhooks"
    assert migration.down_revision == "0051_draw_runtime_config"
    assert RUNTIME_SCHEMA_REVISION == "0052_wxbot_group_webhooks"
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_heads() == [
        RUNTIME_SCHEMA_REVISION
    ]
    assert "plugin_wxbot_group_webhook" in RUNTIME_SCHEMA_TABLES
    assert (
        "ux_plugin_wxbot_group_webhook_active_group",
        "plugin_wxbot_group_webhook",
        ("tenant_id", "connection_id", "external_session_id"),
        "revoked_at IS NULL",
    ) in RUNTIME_SCHEMA_INDEX_CONTRACTS
    assert "CREATE TABLE" in rendered
    assert "plugin_wxbot_group_webhook" in rendered
    assert "ux_plugin_wxbot_group_webhook_active_group" in rendered


def test_group_webhook_downgrade_drops_table(monkeypatch) -> None:
    _migration, rendered = _render(monkeypatch, "downgrade")

    assert "DROP TABLE" in rendered
    assert "plugin_wxbot_group_webhook" in rendered


def test_request_public_origin_prefers_configured_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.common.config import get_settings
    from plugins.wxbot.admin_group_webhook_routes import _request_public_origin

    monkeypatch.setattr(
        get_settings(),
        "wxbot_group_webhook_public_origin",
        "https://hooks.example/",
    )
    request = SimpleNamespace(
        headers={},
        url=SimpleNamespace(scheme="http"),
        base_url="http://127.0.0.1:4173/",
    )
    assert _request_public_origin(request) == "https://hooks.example"


def test_request_public_origin_prefers_configured_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.common.config import get_settings
    from plugins.wxbot.admin_group_webhook_routes import _request_public_origin

    monkeypatch.setattr(
        get_settings(),
        "wxbot_group_webhook_public_origin",
        "https://hooks.example/",
    )
    request = SimpleNamespace(
        headers={},
        url=SimpleNamespace(scheme="http"),
        base_url="http://127.0.0.1:4173/",
    )
    assert _request_public_origin(request) == "https://hooks.example"


def test_webhook_token_helpers_are_unguessable_and_hashed() -> None:
    token = new_webhook_token()
    assert token.startswith("whg_")
    assert len(token) > 20
    assert token_hint(token) == token[-4:]
    assert hash_webhook_token(token) != token
    assert len(hash_webhook_token(token)) == 64
    assert webhook_public_url("https://bot.example/", token) == (
        f"https://bot.example/api/v1/webhook/groups/{token}"
    )
    assert webhook_public_url("https://bot.example/api", token) == (
        f"https://bot.example/api/v1/webhook/groups/{token}"
    )
    assert webhook_public_url("https://bot.example/", token, api_prefix="") == (
        f"https://bot.example/v1/webhook/groups/{token}"
    )


def test_normalize_group_session_ids_requires_chatroom_and_canonicalizes() -> None:
    with pytest.raises(ValueError, match="group_session_required"):
        normalize_group_session_ids("wxid_private", LEGACY_WXBOT_CONNECTION_ID)

    external, canonical = normalize_group_session_ids(
        "room@chatroom",
        LEGACY_WXBOT_CONNECTION_ID,
    )
    assert external == "room@chatroom"
    assert canonical == "room@chatroom"

    external, canonical = normalize_group_session_ids("room@chatroom", "conn-a")
    assert external == "room@chatroom"
    assert canonical == canonical_conversation_id("conn-a", "room@chatroom")
    assert canonical.startswith("cx1:c:")
    assert canonical.endswith("@chatroom")


@pytest.mark.asyncio
async def test_send_group_webhook_text_bypasses_source_bound_contract() -> None:
    captured: dict[str, Any] = {}

    class _Outbound:
        async def send_text(
            self,
            target: ChannelTarget,
            text: str,
            options: ChannelSendOptions | None = None,
        ) -> ChannelSendResult:
            captured["target"] = target
            captured["text"] = text
            captured["options"] = options
            return ChannelSendResult(message_id="77", provider="wxbot")

    result = await send_group_webhook_text(
        _Outbound(),
        _record(),
        text="第三方告警",
        message_id="ext-1",
        trace_id="tr_test",
    )

    assert result.message_id == "77"
    target = captured["target"]
    options = captured["options"]
    assert isinstance(target, ChannelTarget)
    assert target.session_kind == "group"
    assert target.external_conversation_id == "room@chatroom"
    assert captured["text"] == "第三方告警"
    assert options.idempotency_key == "group-webhook:gwh_test:ext-1"
    assert not options.source_message
    assert not options.reply_to_message_id
    delivery = options.delivery_metadata
    assert delivery["force_send"] is True
    assert delivery["speech_budget_enabled"] is False
    assert delivery["duplicate_guard_enabled"] is False
    assert delivery["source"] == "external_group_webhook"
    assert delivery["speech_class"] == "scheduled"
    assert "source_message_id" not in delivery
    assert "participation_status" not in delivery


@pytest.mark.asyncio
async def test_unknown_or_short_token_does_not_hit_store(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_args: object, **_kwargs: object) -> list[dict]:
        raise AssertionError("token lookup must fail closed before SQL")

    monkeypatch.setattr("plugins.wxbot.group_webhook._exec", boom)
    assert await get_group_webhook_by_token("") is None
    assert await get_group_webhook_by_token("nope") is None
    assert await get_group_webhook_by_token("whg_short") is None


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> InMemoryRedis:
    redis = InMemoryRedis()
    monkeypatch.setattr("app.infra.redis_client.get_redis", lambda: redis)
    monkeypatch.setattr("plugins.wxbot.group_webhook_router.get_redis", lambda: redis)
    return redis


def _public_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    record: GroupWebhookRecord | None,
    send_result: ChannelSendResult | None = None,
    send_error: Exception | None = None,
) -> tuple[TestClient, dict[str, Any]]:
    calls: dict[str, Any] = {"send": 0, "touch": 0}

    async def fake_lookup(_token: str) -> GroupWebhookRecord | None:
        return record

    async def fake_send(*_args: object, **_kwargs: object) -> ChannelSendResult:
        calls["send"] += 1
        if send_error is not None:
            raise send_error
        return send_result or ChannelSendResult(message_id="42", provider="wxbot")

    async def fake_touch(_webhook_id: str) -> None:
        calls["touch"] += 1

    monkeypatch.setattr(
        "plugins.wxbot.group_webhook_router.get_group_webhook_by_token",
        fake_lookup,
    )
    monkeypatch.setattr(
        "plugins.wxbot.group_webhook_router.send_group_webhook_text",
        fake_send,
    )
    monkeypatch.setattr(
        "plugins.wxbot.group_webhook_router.touch_group_webhook",
        fake_touch,
    )
    container = SimpleNamespace(wxbot_store=WxbotStore(SimpleNamespace()))
    app = FastAPI()
    app.include_router(build_group_webhook_router(container))
    return TestClient(app), calls


def test_public_group_webhook_accepts_text_push(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: InMemoryRedis,
) -> None:
    record = _record()
    client, calls = _public_client(monkeypatch, record=record)
    token = new_webhook_token()

    response = client.post(
        f"/v1/webhook/groups/{token}",
        content=orjson.dumps({"text": "  hello from pagerduty  ", "message_id": "alert-1"}),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] == "accepted"
    assert payload["webhook_id"] == "gwh_test"
    assert payload["queue_id"] == "42"
    assert payload["trace_id"]
    assert calls["send"] == 1
    assert calls["touch"] == 1


def test_public_group_webhook_unknown_token_is_404(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: InMemoryRedis,
) -> None:
    client, calls = _public_client(monkeypatch, record=None)
    response = client.post(
        f"/v1/webhook/groups/{new_webhook_token()}",
        json={"text": "hello"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "webhook_not_found"
    assert calls["send"] == 0


def test_public_group_webhook_revoked_token_is_404(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: InMemoryRedis,
) -> None:
    from datetime import UTC, datetime

    client, calls = _public_client(
        monkeypatch,
        record=_record(enabled=False, revoked_at=datetime.now(UTC)),
    )
    response = client.post(
        f"/v1/webhook/groups/{new_webhook_token()}",
        json={"text": "hello"},
    )
    assert response.status_code == 404
    assert calls["send"] == 0


def test_public_group_webhook_replays_message_id(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: InMemoryRedis,
) -> None:
    client, calls = _public_client(monkeypatch, record=_record())
    token = new_webhook_token()
    body = {"text": "same alert", "message_id": "alert-9"}
    first = client.post(f"/v1/webhook/groups/{token}", json=body)
    second = client.post(f"/v1/webhook/groups/{token}", json=body)
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate", "webhook_id": "gwh_test"}
    assert calls["send"] == 1


def test_public_group_webhook_idempotency_header(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: InMemoryRedis,
) -> None:
    client, calls = _public_client(monkeypatch, record=_record())
    token = new_webhook_token()
    headers = {"Idempotency-Key": "hdr-1"}
    first = client.post(f"/v1/webhook/groups/{token}", json={"text": "one"}, headers=headers)
    second = client.post(f"/v1/webhook/groups/{token}", json={"text": "two"}, headers=headers)
    assert first.status_code == 202
    assert second.status_code == 200
    assert calls["send"] == 1


def test_public_group_webhook_rejects_empty_text(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: InMemoryRedis,
) -> None:
    client, calls = _public_client(monkeypatch, record=_record())
    response = client.post(
        f"/v1/webhook/groups/{new_webhook_token()}",
        json={"text": "   "},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "text_required"
    assert calls["send"] == 0


def test_public_group_webhook_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: InMemoryRedis,
) -> None:
    from app.common.config import get_settings

    monkeypatch.setattr(get_settings(), "inbound_default_rate_limit", 1)
    client, calls = _public_client(monkeypatch, record=_record())
    token = new_webhook_token()
    first = client.post(f"/v1/webhook/groups/{token}", json={"text": "one"})
    second = client.post(f"/v1/webhook/groups/{token}", json={"text": "two"})
    assert first.status_code == 202
    assert second.status_code == 429
    assert second.json()["detail"] == "rate_limited"
    assert calls["send"] == 1


def _admin_client(scope_allowed: bool = True) -> TestClient:
    async def scope_gate(_tenant_id: str, _session_id: str) -> bool:
        return scope_allowed

    app = FastAPI()
    app.include_router(
        build_wxbot_router(
            _FakeStore(),
            container=None,
            bridge=_FakeBridge(),
            scope_execution_allowed=scope_gate,
        )
    )
    return TestClient(app)


@pytest.mark.asyncio
async def test_admin_group_webhook_create_list_rotate_revoke(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'group-webhooks.db'}")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE plugin_wxbot_group_webhook (
                    webhook_id VARCHAR(64) NOT NULL PRIMARY KEY,
                    tenant_id VARCHAR(64) NOT NULL,
                    connection_id VARCHAR(64) NOT NULL DEFAULT '',
                    external_session_id VARCHAR(256) NOT NULL,
                    canonical_session_id VARCHAR(256) NOT NULL,
                    session_name VARCHAR(256) NOT NULL DEFAULT '',
                    token_hash VARCHAR(64) NOT NULL UNIQUE,
                    token_hint VARCHAR(8) NOT NULL DEFAULT '',
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    created_by VARCHAR(128) NOT NULL DEFAULT '',
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_used_at DATETIME,
                    revoked_at DATETIME
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX ux_plugin_wxbot_group_webhook_active_group
                ON plugin_wxbot_group_webhook (tenant_id, connection_id, external_session_id)
                WHERE revoked_at IS NULL
                """
            )
        )
    monkeypatch.setattr("plugins.wxbot.store.get_engine", lambda: engine)

    client = _admin_client()
    headers = {"Authorization": "Bearer token"}
    created = client.post(
        "/admin/group-webhooks",
        headers=headers,
        json={
            "tenant_id": "default",
            "session_id": "room@chatroom",
            "session_name": "测试群",
        },
    )
    assert created.status_code == 200, created.text
    created_payload = created.json()
    assert created_payload["token"].startswith("whg_")
    assert created_payload["url"].endswith(f"/api/v1/webhook/groups/{created_payload['token']}")
    assert created_payload["path"] == f"/api/v1/webhook/groups/{created_payload['token']}"
    assert created_payload["example"]["body"]["text"] == "hello"
    assert "token_hash" not in created_payload
    first_token = created_payload["token"]

    duplicate = client.post(
        "/admin/group-webhooks",
        headers=headers,
        json={"tenant_id": "default", "session_id": "room@chatroom"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "group_webhook_exists"

    listed = client.get("/admin/group-webhooks", headers=headers, params={"tenant_id": "default"})
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert listed.json()["count"] == 1
    assert items[0]["session_id"] == "room@chatroom"
    assert items[0]["token_hint"] == first_token[-4:]
    assert "token" not in items[0]
    assert items[0]["enabled"] is True

    rotated = client.post(
        f"/admin/group-webhooks/{created_payload['webhook_id']}/rotate",
        headers=headers,
    )
    assert rotated.status_code == 200, rotated.text
    rotated_token = rotated.json()["token"]
    assert rotated_token != first_token
    assert rotated.json()["token_hint"] == rotated_token[-4:]

    revoked = client.delete(
        f"/admin/group-webhooks/{created_payload['webhook_id']}",
        headers=headers,
    )
    assert revoked.status_code == 200
    assert revoked.json()["enabled"] is False
    assert "token" not in revoked.json()

    listed_after = client.get(
        "/admin/group-webhooks",
        headers=headers,
        params={"tenant_id": "default"},
    )
    assert listed_after.json()["items"][0]["enabled"] is False

    recreated = client.post(
        "/admin/group-webhooks",
        headers=headers,
        json={"tenant_id": "default", "session_id": "room@chatroom"},
    )
    assert recreated.status_code == 200, recreated.text
    assert recreated.json()["token"] != rotated_token

    purged = client.delete(
        f"/admin/group-webhooks/{created_payload['webhook_id']}",
        headers=headers,
    )
    assert purged.status_code == 200, purged.text
    assert purged.json() == {
        "deleted": True,
        "webhook_id": created_payload["webhook_id"],
    }
    listed_purged = client.get(
        "/admin/group-webhooks",
        headers=headers,
        params={"tenant_id": "default"},
    )
    leftover = listed_purged.json()["items"]
    assert listed_purged.json()["count"] == 1
    assert leftover[0]["webhook_id"] == recreated.json()["webhook_id"]
    assert leftover[0]["enabled"] is True

    missing = client.delete(
        f"/admin/group-webhooks/{created_payload['webhook_id']}",
        headers=headers,
    )
    assert missing.status_code == 404
    await engine.dispose()


def test_admin_group_webhook_rejects_private_session() -> None:
    response = _admin_client().post(
        "/admin/group-webhooks",
        headers={"Authorization": "Bearer token"},
        json={"tenant_id": "default", "session_id": "wxid_private"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "group session required"


def test_admin_group_webhook_requires_verified_roster_group() -> None:
    response = _admin_client().post(
        "/admin/group-webhooks",
        headers={"Authorization": "Bearer token"},
        json={"tenant_id": "default", "session_id": "missing@chatroom"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "target group is not present in verified roster"


def test_admin_group_webhook_requires_scope_gate() -> None:
    response = _admin_client(scope_allowed=False).post(
        "/admin/group-webhooks",
        headers={"Authorization": "Bearer token"},
        json={"tenant_id": "default", "session_id": "room@chatroom"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "plugin_runtime_disabled"


def test_public_document_never_exposes_token_material() -> None:
    document = public_webhook_document(_record(token_hash="secret-hash"))
    assert document["token_hint"] == "wxyz"
    assert "token" not in document
    assert "token_hash" not in document
    assert document["path_template"] == "/api/v1/webhook/groups/{token}"
