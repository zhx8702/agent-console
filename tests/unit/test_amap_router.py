from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import create_async_engine

from app.admin.audit import AdminAuditEvent, install_admin_audit_middleware
from app.admin.mutation_ledger import plugin_admin_mutation_idempotency
from app.common.config import Settings
from plugins.amap.config_mutations import AMapConfigMutationStore
from plugins.amap.router import build_amap_router


@dataclass
class _AuditSink:
    events: list[AdminAuditEvent] = field(default_factory=list)

    async def write(self, event: AdminAuditEvent) -> None:
        self.events.append(event)


def _app(
    settings: Settings,
    *,
    mutation_store: AMapConfigMutationStore | None = None,
    audit_sink: _AuditSink | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        build_amap_router(settings, mutation_store),
        prefix="/plugins/amap",
    )
    if audit_sink is not None:
        install_admin_audit_middleware(app, settings, sink=audit_sink)
    return app


@pytest_asyncio.fixture
async def amap_mutation_store(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'amap-ledger.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(plugin_admin_mutation_idempotency.metadata.create_all)
    yield AMapConfigMutationStore(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_amap_config_uses_custom_env_file_and_preserves_unrelated_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    amap_mutation_store: AMapConfigMutationStore,
) -> None:
    from plugins.amap import router as amap_router

    env_path = tmp_path / "runtime.env"
    env_path.write_text(
        "UNRELATED_SETTING=keep\nAMAP_API_TIMEOUT_SECONDS=30\n",
        encoding="utf-8",
    )
    storage_dir = tmp_path / "maps"
    storage_dir.mkdir()
    monkeypatch.setenv("AGENT_CONSOLE_ENV_FILE", str(env_path))
    monkeypatch.delenv("AMAP_API_TIMEOUT_SECONDS", raising=False)
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        amap_storage_dir=str(storage_dir),
    )
    monkeypatch.setattr(amap_router, "_load_runtime_settings", lambda: settings)
    transport = httpx.ASGITransport(
        app=_app(settings, mutation_store=amap_mutation_store)
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        current = await client.get(
            "/plugins/amap/admin/config",
            headers={"Authorization": "Bearer unit_admin_token"},
        )
        response = await client.post(
            "/plugins/amap/admin/config",
            headers={
                "Authorization": "Bearer unit_admin_token",
                "If-Match": current.headers["etag"],
                "Idempotency-Key": "amap-timeout-save-1",
            },
            json={"timeout_seconds": 4.5},
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["etag"] != current.headers["etag"]
    assert "settings_file" not in response.json()
    assert response.json()["api_key_mutable_via_api"] is False
    content = env_path.read_text(encoding="utf-8")
    assert "UNRELATED_SETTING=keep" in content
    assert "AMAP_API_TIMEOUT_SECONDS=4.5" in content
    assert "AMAP_API_KEY=" not in content
    assert "AMAP_STORAGE_DIR=" not in content
    assert "AMAP_API_TIMEOUT_SECONDS" not in os.environ


@pytest.mark.asyncio
async def test_amap_config_requires_if_match_and_rejects_stale_writers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    amap_mutation_store: AMapConfigMutationStore,
) -> None:
    from plugins.amap import router as amap_router

    env_path = tmp_path / "runtime.env"
    env_path.write_text("AMAP_API_TIMEOUT_SECONDS=30\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CONSOLE_ENV_FILE", str(env_path))
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        amap_api_timeout_seconds=30,
        amap_storage_dir=str(tmp_path / "maps"),
    )
    monkeypatch.setattr(amap_router, "_load_runtime_settings", lambda: settings)
    transport = httpx.ASGITransport(
        app=_app(settings, mutation_store=amap_mutation_store)
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing = await client.post(
            "/plugins/amap/admin/config",
            headers={
                "Authorization": "Bearer unit_admin_token",
                "Idempotency-Key": "amap-missing-version",
            },
            json={"timeout_seconds": 5},
        )
        current = await client.get(
            "/plugins/amap/admin/config",
            headers={"Authorization": "Bearer unit_admin_token"},
        )
        first = await client.post(
            "/plugins/amap/admin/config",
            headers={
                "Authorization": "Bearer unit_admin_token",
                "If-Match": current.headers["etag"],
                "Idempotency-Key": "amap-first-writer",
            },
            json={"timeout_seconds": 5},
        )
        stale = await client.post(
            "/plugins/amap/admin/config",
            headers={
                "Authorization": "Bearer unit_admin_token",
                "If-Match": current.headers["etag"],
                "Idempotency-Key": "amap-stale-writer",
            },
            json={"timeout_seconds": 9},
        )

    assert missing.status_code == 428
    assert missing.json()["detail"] == "if_match_required"
    assert first.status_code == 200
    assert stale.status_code == 409
    assert stale.headers["etag"] == first.headers["etag"]
    assert stale.json()["detail"]["code"] == "amap_config_version_conflict"
    assert env_path.read_text(encoding="utf-8").count("AMAP_API_TIMEOUT_SECONDS=5.0") == 1
    assert "AMAP_API_TIMEOUT_SECONDS=9" not in env_path.read_text(encoding="utf-8")


def test_amap_config_compare_and_set_has_one_winner_across_concurrent_writers(
    tmp_path,
) -> None:
    from plugins.amap import router as amap_router

    env_path = tmp_path / "runtime.env"
    env_path.write_text("AMAP_API_TIMEOUT_SECONDS=30\n", encoding="utf-8")
    etag = amap_router._config_etag(str(env_path))
    barrier = threading.Barrier(2)

    def write(value: int) -> tuple[str, str]:
        barrier.wait(timeout=5)
        try:
            next_etag = amap_router._write_env_overrides_if_match(
                str(env_path),
                {"AMAP_API_TIMEOUT_SECONDS": value},
                expected_etag=etag,
            )
            return "success", next_etag
        except HTTPException as exc:
            return "conflict", str(exc.status_code)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, (5, 9)))

    assert sorted(status for status, _ in results) == ["conflict", "success"]
    assert next(value for status, value in results if status == "conflict") == "409"
    content = env_path.read_text(encoding="utf-8")
    assert ("AMAP_API_TIMEOUT_SECONDS=5\n" in content) ^ (
        "AMAP_API_TIMEOUT_SECONDS=9\n" in content
    )


@pytest.mark.asyncio
async def test_amap_api_key_is_external_only_and_never_echoed_or_audited(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    amap_mutation_store: AMapConfigMutationStore,
) -> None:
    from plugins.amap import router as amap_router

    env_path = tmp_path / "runtime.env"
    env_path.write_text("AMAP_API_TIMEOUT_SECONDS=30\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CONSOLE_ENV_FILE", str(env_path))
    existing_secret = "existing-amap-secret"
    submitted_secret = "submitted-amap-secret"
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        amap_api_key=existing_secret,
        amap_api_timeout_seconds=30,
        amap_storage_dir=str(tmp_path / "maps"),
    )
    monkeypatch.setattr(amap_router, "_load_runtime_settings", lambda: settings)
    sink = _AuditSink()
    transport = httpx.ASGITransport(
        app=_app(
            settings,
            mutation_store=amap_mutation_store,
            audit_sink=sink,
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        current = await client.get(
            "/plugins/amap/admin/config",
            headers={"Authorization": "Bearer unit_admin_token"},
        )
        rejected = await client.post(
            "/plugins/amap/admin/config",
            headers={
                "Authorization": "Bearer unit_admin_token",
                "If-Match": current.headers["etag"],
                "Idempotency-Key": "amap-secret-rejected",
            },
            json={"amap_api_key": submitted_secret},
        )
        updated = await client.post(
            "/plugins/amap/admin/config",
            headers={
                "Authorization": "Bearer unit_admin_token",
                "If-Match": current.headers["etag"],
                "Idempotency-Key": "amap-safe-update",
            },
            json={"timeout_seconds": 8},
        )

    assert current.status_code == 200
    assert current.json()["api_key_configured"] is True
    assert existing_secret not in current.text
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "amap_api_key_managed_by_secret_provider"
    assert submitted_secret not in rejected.text
    assert submitted_secret not in env_path.read_text(encoding="utf-8")
    assert updated.status_code == 200
    success_event = next(event for event in sink.events if event.status == 200)
    assert success_event.target_type == "plugin:amap:runtime_config"
    rendered_audit = str(success_event.as_dict())
    assert existing_secret not in rendered_audit
    assert submitted_secret not in rendered_audit
    assert str(tmp_path) not in rendered_audit


@pytest.mark.asyncio
async def test_amap_config_rejects_fields_owned_by_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    amap_mutation_store: AMapConfigMutationStore,
) -> None:
    from plugins.amap import router as amap_router

    env_path = tmp_path / "runtime.env"
    env_path.write_text("AMAP_API_TIMEOUT_SECONDS=30\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CONSOLE_ENV_FILE", str(env_path))
    monkeypatch.setenv("AMAP_API_TIMEOUT_SECONDS", "12")
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        amap_storage_dir=str(tmp_path / "maps"),
    )
    monkeypatch.setattr(amap_router, "_load_runtime_settings", lambda: settings)
    transport = httpx.ASGITransport(
        app=_app(settings, mutation_store=amap_mutation_store)
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        current = await client.get(
            "/plugins/amap/admin/config",
            headers={"Authorization": "Bearer unit_admin_token"},
        )
        rejected = await client.post(
            "/plugins/amap/admin/config",
            headers={
                "Authorization": "Bearer unit_admin_token",
                "If-Match": current.headers["etag"],
                "Idempotency-Key": "amap-env-managed",
            },
            json={"timeout_seconds": 5},
        )

    assert rejected.status_code == 409
    assert rejected.json()["detail"] == {
        "code": "amap_config_field_managed_externally",
        "fields": ["AMAP_API_TIMEOUT_SECONDS"],
    }
    assert env_path.read_text(encoding="utf-8") == "AMAP_API_TIMEOUT_SECONDS=30\n"


@pytest.mark.asyncio
async def test_amap_config_is_immutable_and_hides_env_path_in_production(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from plugins.amap import router as amap_router

    env_path = tmp_path / "production.env"
    original = "AMAP_API_TIMEOUT_SECONDS=30\n"
    env_path.write_text(original, encoding="utf-8")
    monkeypatch.setenv("AGENT_CONSOLE_ENV_FILE", str(env_path))
    settings = Settings(
        app_env="prod",
        admin_bearer_token="unit_admin_token",
        amap_storage_dir=str(tmp_path / "maps"),
    )
    monkeypatch.setenv("AMAP_API_TIMEOUT_SECONDS", "sentinel")

    def unexpected_load() -> Settings:
        raise AssertionError("production mutation must reject before loading mutable config")

    monkeypatch.setattr(amap_router, "_load_runtime_settings", unexpected_load)
    transport = httpx.ASGITransport(app=_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        rejected = await client.post(
            "/plugins/amap/admin/config",
            headers={"Authorization": "Bearer unit_admin_token"},
            json={"timeout_seconds": 4.5},
        )
        monkeypatch.setattr(amap_router, "_load_runtime_settings", lambda: settings)
        visible = await client.get(
            "/plugins/amap/admin/config",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "runtime_config_read_only_in_production"
    assert str(env_path) not in rejected.text
    assert env_path.read_text(encoding="utf-8") == original
    assert os.environ["AMAP_API_TIMEOUT_SECONDS"] == "sentinel"
    assert not (tmp_path / ".production.env.lock").exists()
    assert visible.status_code == 200
    assert "settings_file" not in visible.json()
    assert visible.json()["runtime_config_mutable"] is False
    assert not (tmp_path / "maps").exists()


@pytest.mark.asyncio
async def test_amap_file_route_only_serves_generated_png_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from plugins.amap import router as amap_router

    storage_dir = tmp_path / "maps"
    storage_dir.mkdir()
    (storage_dir / "amap-safe-trace.png").write_bytes(b"png")
    (storage_dir / ".env").write_text("AMAP_API_KEY=must-not-leak\n", encoding="utf-8")
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        amap_storage_dir=str(storage_dir),
    )
    monkeypatch.setattr(amap_router, "_load_runtime_settings", lambda: settings)
    transport = httpx.ASGITransport(app=_app(settings))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        allowed = await client.get(
            "/plugins/amap/files/amap-safe-trace.png",
            headers={"Authorization": "Bearer unit_admin_token"},
        )
        secret = await client.get(
            "/plugins/amap/files/.env",
            headers={"Authorization": "Bearer unit_admin_token"},
        )
        traversal = await client.get(
            "/plugins/amap/files/../.env",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert allowed.status_code == 200
    assert allowed.content == b"png"
    assert secret.status_code == 404
    assert traversal.status_code == 404
    assert "must-not-leak" not in secret.text + traversal.text
