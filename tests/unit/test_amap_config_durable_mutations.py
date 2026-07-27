from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.admin.mutation_ledger import (
    plugin_admin_mutation_audit,
    plugin_admin_mutation_idempotency,
)
from app.common.config import Settings
from plugins.amap.config_mutations import AMapConfigMutationStore
from plugins.amap.router import build_amap_router


async def _store(tmp_path, name: str) -> tuple[AMapConfigMutationStore, AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    async with engine.begin() as conn:
        await conn.run_sync(plugin_admin_mutation_idempotency.metadata.create_all)
    return AMapConfigMutationStore(engine), engine


def _app(settings: Settings, store: AMapConfigMutationStore) -> FastAPI:
    app = FastAPI()
    app.include_router(
        build_amap_router(settings, store),
        prefix="/plugins/amap",
    )
    return app


def _settings(tmp_path) -> Settings:
    return Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        amap_api_key="existing-secret-never-persist-in-audit",
        amap_api_timeout_seconds=30,
        amap_storage_dir=str(tmp_path / "initial-maps"),
    )


async def _etag(client: httpx.AsyncClient) -> str:
    current = await client.get(
        "/plugins/amap/admin/config",
        headers={"Authorization": "Bearer unit_admin_token"},
    )
    assert current.status_code == 200
    return current.headers["etag"]


@pytest.mark.asyncio
async def test_amap_config_requires_stable_key_exactly_replays_and_audits_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from plugins.amap import router as amap_router

    env_path = tmp_path / "runtime.env"
    env_path.write_text(
        "UNRELATED=keep\nAMAP_API_TIMEOUT_SECONDS=30\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_CONSOLE_ENV_FILE", str(env_path))
    monkeypatch.delenv("AMAP_API_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AMAP_STORAGE_DIR", raising=False)
    settings = _settings(tmp_path)
    monkeypatch.setattr(amap_router, "_load_runtime_settings", lambda: settings)
    store, engine = await _store(tmp_path, "exact-replay.db")
    transport = httpx.ASGITransport(app=_app(settings, store))
    private_storage_path = str(tmp_path / "private-user-storage")

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            etag = await _etag(client)
            missing = await client.post(
                "/plugins/amap/admin/config",
                headers={
                    "Authorization": "Bearer unit_admin_token",
                    "If-Match": etag,
                },
                json={"timeout_seconds": 7},
            )
            invalid = await client.post(
                "/plugins/amap/admin/config",
                headers={
                    "Authorization": "Bearer unit_admin_token",
                    "If-Match": etag,
                    "Idempotency-Key": "x" * 129,
                },
                json={"timeout_seconds": 7},
            )
            headers = {
                "Authorization": "Bearer unit_admin_token",
                "If-Match": etag,
                "Idempotency-Key": "amap-lost-response-intent",
                "X-Trace-ID": "trace-amap-config-1",
            }
            body = {
                "timeout_seconds": 7,
                "storage_dir": private_storage_path,
            }
            first = await client.post(
                "/plugins/amap/admin/config",
                headers=headers,
                json=body,
            )
            # Exact replay is resolved before re-evaluating mutable ownership;
            # a later process override must not turn a lost response into a
            # second intent or a different result.
            monkeypatch.setenv("AMAP_API_TIMEOUT_SECONDS", "99")
            replay = await client.post(
                "/plugins/amap/admin/config",
                headers=headers,
                json=body,
            )
            conflicting_reuse = await client.post(
                "/plugins/amap/admin/config",
                headers=headers,
                json={"timeout_seconds": 9, "storage_dir": private_storage_path},
            )

        assert missing.status_code == 428
        assert missing.json()["detail"] == {"code": "idempotency_key_required"}
        assert invalid.status_code == 400
        assert invalid.json()["detail"] == {"code": "invalid_idempotency_key"}
        assert first.status_code == replay.status_code == 200
        assert first.json() == replay.json()
        assert first.headers["etag"] == replay.headers["etag"]
        assert first.headers["x-mutation-id"] == replay.headers["x-mutation-id"]
        assert replay.headers["idempotent-replayed"] == "true"
        assert conflicting_reuse.status_code == 409
        assert conflicting_reuse.json()["detail"] == {
            "code": "idempotency_key_conflict"
        }
        content = env_path.read_text(encoding="utf-8")
        assert content.count("AMAP_API_TIMEOUT_SECONDS=7.0") == 1
        assert content.count(" # agent-console-amap-mutation=") == 1
        assert "amap-lost-response-intent" not in content
        assert "UNRELATED=keep" in content

        async with engine.connect() as conn:
            rows = (
                await conn.execute(select(plugin_admin_mutation_idempotency))
            ).mappings().all()
            audits = (
                await conn.execute(select(plugin_admin_mutation_audit))
            ).mappings().all()
        assert len(rows) == len(audits) == 1
        assert rows[0]["completed_at"] is not None
        assert rows[0]["idempotency_key_hash"] != "amap-lost-response-intent"
        assert "existing-secret-never-persist-in-audit" not in str(dict(rows[0]))
        assert audits[0]["scope_json"] == {
            "timeout_changed": True,
            "storage_dir_changed": True,
        }
        assert audits[0]["before_state_json"] == {
            "api_key_configured": True,
            "timeout_seconds": 30.0,
            "storage_dir_configured": True,
        }
        assert audits[0]["after_state_json"]["timeout_seconds"] == 7.0
        audit_blob = str(dict(audits[0]))
        assert private_storage_path not in audit_blob
        assert "existing-secret-never-persist-in-audit" not in audit_blob
        assert "trace-amap-config-1" in audit_blob
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_after_replace", [False, True])
async def test_prepared_file_failure_recovers_without_duplicating_the_effect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    fail_after_replace: bool,
) -> None:
    from plugins.amap import router as amap_router

    env_path = tmp_path / "runtime.env"
    env_path.write_text("AMAP_API_TIMEOUT_SECONDS=30\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CONSOLE_ENV_FILE", str(env_path))
    monkeypatch.delenv("AMAP_API_TIMEOUT_SECONDS", raising=False)
    settings = _settings(tmp_path)
    monkeypatch.setattr(amap_router, "_load_runtime_settings", lambda: settings)
    store, engine = await _store(tmp_path, f"file-failure-{fail_after_replace}.db")
    transport = httpx.ASGITransport(app=_app(settings, store))
    real_write = amap_router._write_env_overrides_with_marker
    successful_replaces = 0

    def counted_write(
        env_path_value: str,
        values: dict[str, Any],
        *,
        mutation_marker: str,
    ) -> None:
        nonlocal successful_replaces
        real_write(
            env_path_value,
            values,
            mutation_marker=mutation_marker,
        )
        successful_replaces += 1

    def injected_write(
        env_path_value: str,
        values: dict[str, Any],
        *,
        mutation_marker: str,
    ) -> None:
        if fail_after_replace:
            counted_write(
                env_path_value,
                values,
                mutation_marker=mutation_marker,
            )
        raise OSError("injected dotenv failure")

    monkeypatch.setattr(
        amap_router,
        "_write_env_overrides_with_marker",
        injected_write,
    )
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            etag = await _etag(client)
            headers = {
                "Authorization": "Bearer unit_admin_token",
                "If-Match": etag,
                "Idempotency-Key": f"amap-file-failure-{fail_after_replace}",
            }
            pending = await client.post(
                "/plugins/amap/admin/config",
                headers=headers,
                json={"timeout_seconds": 6},
            )
            monkeypatch.setattr(
                amap_router,
                "_write_env_overrides_with_marker",
                counted_write,
            )
            recovered = await client.post(
                "/plugins/amap/admin/config",
                headers=headers,
                json={"timeout_seconds": 6},
            )

        assert pending.status_code == 503
        assert pending.json()["detail"]["code"] == "amap_config_mutation_pending"
        assert recovered.status_code == 200
        assert recovered.json()["timeout_seconds"] == 6.0
        assert recovered.headers["x-mutation-id"] == pending.json()["detail"][
            "mutation_id"
        ]
        # Before-replace failure writes on retry; after-replace failure is
        # recovered from the durable marker without another replace.
        assert successful_replaces == 1
        assert env_path.read_text(encoding="utf-8").count(
            "AMAP_API_TIMEOUT_SECONDS=6.0"
        ) == 1
        async with engine.connect() as conn:
            row = (
                await conn.execute(select(plugin_admin_mutation_idempotency))
            ).mappings().one()
            audit_count = len(
                (await conn.execute(select(plugin_admin_mutation_audit))).all()
            )
        assert row["completed_at"] is not None
        assert audit_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_completion_loss_then_intervening_write_is_durably_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from plugins.amap import router as amap_router

    env_path = tmp_path / "runtime.env"
    env_path.write_text("AMAP_API_TIMEOUT_SECONDS=30\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CONSOLE_ENV_FILE", str(env_path))
    monkeypatch.delenv("AMAP_API_TIMEOUT_SECONDS", raising=False)
    settings = _settings(tmp_path)
    monkeypatch.setattr(amap_router, "_load_runtime_settings", lambda: settings)
    store, engine = await _store(tmp_path, "indeterminate.db")
    transport = httpx.ASGITransport(app=_app(settings, store))
    real_complete = store.complete_success

    async def lose_completion(_mutation_id: str):
        raise RuntimeError("injected database completion loss")

    monkeypatch.setattr(store, "complete_success", lose_completion)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            etag = await _etag(client)
            headers = {
                "Authorization": "Bearer unit_admin_token",
                "If-Match": etag,
                "Idempotency-Key": "amap-completion-loss",
            }
            pending = await client.post(
                "/plugins/amap/admin/config",
                headers=headers,
                json={"timeout_seconds": 8},
            )
            assert pending.status_code == 503
            mutation_id = pending.json()["detail"]["mutation_id"]

            # A later writer changes both the state and attribution marker.
            amap_router._write_env_overrides_with_marker(
                str(env_path),
                {"AMAP_API_TIMEOUT_SECONDS": 11},
                mutation_marker="a" * 64,
            )
            monkeypatch.setattr(store, "complete_success", real_complete)
            indeterminate = await client.post(
                "/plugins/amap/admin/config",
                headers=headers,
                json={"timeout_seconds": 8},
            )
            durable_replay = await client.post(
                "/plugins/amap/admin/config",
                headers=headers,
                json={"timeout_seconds": 8},
            )

        expected_detail = {
            "code": "amap_config_mutation_indeterminate",
            "mutation_id": mutation_id,
        }
        assert indeterminate.status_code == durable_replay.status_code == 409
        assert indeterminate.json()["detail"] == expected_detail
        assert durable_replay.json()["detail"] == expected_detail
        assert "AMAP_API_TIMEOUT_SECONDS=11" in env_path.read_text(encoding="utf-8")
        async with engine.connect() as conn:
            row = (
                await conn.execute(select(plugin_admin_mutation_idempotency))
            ).mappings().one()
            audits = (await conn.execute(select(plugin_admin_mutation_audit))).all()
        assert row["completed_at"] is None
        assert row["response_status_code"] == 409
        assert row["response_json"]["state"] == "indeterminate"
        assert audits == []
    finally:
        await engine.dispose()
