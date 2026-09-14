from __future__ import annotations

from importlib import import_module
from io import StringIO

import httpx
import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from fastapi import FastAPI

from app.infra.runtime_schema import RUNTIME_SCHEMA_TABLES
from plugins.draw.router import build_draw_router
from plugins.draw.store import DrawStore
from tests.unit._schema_fixtures import bootstrap_draw_task_schema
from tests.unit.test_draw_store import _draw_settings, _SqliteSessionFactory

_MODULE = "migrations.versions.20260909_0051_draw_runtime_config"


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


def test_draw_runtime_config_upgrade_is_runtime_head(monkeypatch) -> None:
    migration, rendered = _render(monkeypatch, "upgrade")

    assert migration.revision == "0051_draw_runtime_config"
    assert migration.down_revision == "0050_speaker_portrait_cursor"
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_revision(
        "0052_wxbot_group_webhooks"
    ).down_revision == migration.revision
    assert "plugin_draw_runtime_config" in RUNTIME_SCHEMA_TABLES
    assert "CREATE TABLE" in rendered
    assert "plugin_draw_runtime_config" in rendered


def test_draw_runtime_config_downgrade_drops_table(monkeypatch) -> None:
    _migration, rendered = _render(monkeypatch, "downgrade")

    assert "DROP TABLE" in rendered
    assert "plugin_draw_runtime_config" in rendered


@pytest.fixture
async def draw_runtime_client(tmp_path):
    factory = _SqliteSessionFactory()
    bootstrap_draw_task_schema(factory._connection)
    settings = _draw_settings(
        tmp_path,
        app_env="test",
        admin_bearer_token="admin_token",
        draw_api_url="https://airgate.example/v1",
        draw_api_key="sk-env-secret",
        draw_api_model="env-model",
    )
    store = DrawStore(settings, session_factory=factory)
    app = FastAPI()
    app.include_router(build_draw_router(store), prefix="/plugins/draw")
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    try:
        yield client, store
    finally:
        await client.aclose()
        await store.close()
        factory.close()


@pytest.mark.asyncio
async def test_draw_runtime_config_api_redacts_key_and_requires_if_match(
    draw_runtime_client,
) -> None:
    client, _store = draw_runtime_client
    auth = {"Authorization": "Bearer admin_token"}

    denied = await client.get("/plugins/draw/admin/config")
    assert denied.status_code == 401

    loaded = await client.get("/plugins/draw/admin/config", headers=auth)
    assert loaded.status_code == 200
    body = loaded.json()
    assert body["enabled"] is True
    assert body["api_url"] == "https://airgate.example/v1"
    assert body["api_key_configured"] is True
    assert "api_key" not in body
    assert "sk-env-secret" not in loaded.text
    assert loaded.headers["etag"] == '"0"'

    missing = await client.post(
        "/plugins/draw/admin/config",
        headers=auth,
        json={"api_url": ""},
    )
    assert missing.status_code == 428

    stale = await client.post(
        "/plugins/draw/admin/config",
        headers={**auth, "If-Match": '"9"'},
        json={"api_url": ""},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "version_conflict"

    disabled = await client.post(
        "/plugins/draw/admin/config",
        headers={**auth, "If-Match": '"0"'},
        json={"api_url": ""},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["api_url"] == ""
    assert disabled.headers["etag"] == '"1"'
    assert "sk-env-secret" not in disabled.text

    updated = await client.post(
        "/plugins/draw/admin/config",
        headers={**auth, "If-Match": '"1"'},
        json={
            "api_url": "https://live.example/v1",
            "api_key": "sk-live-secret",
            "api_model": "grok-imagine-image",
        },
    )
    assert updated.status_code == 200
    payload = updated.json()
    assert payload["enabled"] is True
    assert payload["api_url"] == "https://live.example/v1"
    assert payload["api_model"] == "grok-imagine-image"
    assert payload["api_key_configured"] is True
    assert payload["api_key_hint"] == "cret"
    assert "sk-live-secret" not in updated.text
    assert updated.headers["etag"] == '"2"'
