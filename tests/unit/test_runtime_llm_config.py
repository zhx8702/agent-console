from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.admin.audit import AdminAuditEvent, install_admin_audit_middleware
from app.admin.kb_router import build_admin_router
from app.common.config import Settings
from app.common.runtime_llm_config import (
    RuntimeLlmConfigIdempotencyConflict,
    RuntimeLlmConfigMutation,
    RuntimeLlmConfigSnapshot,
    RuntimeLlmConfigStore,
    RuntimeLlmConfigVersionConflict,
    normalize_runtime_llm_overrides,
    resolve_runtime_llm_config,
    runtime_llm_config_history_table,
    runtime_llm_config_idempotency_table,
    runtime_llm_config_table,
    runtime_llm_overlay_enabled_for_role,
    runtime_llm_request_hash,
)


class _MemoryRuntimeLlmStore:
    def __init__(self, *, fail_reads: bool = False) -> None:
        self.snapshot = RuntimeLlmConfigSnapshot(version=0, overrides={})
        self.fail_reads = fail_reads
        self.lock = asyncio.Lock()
        self.idempotency: dict[str, tuple[str, RuntimeLlmConfigSnapshot]] = {}

    async def get(self) -> RuntimeLlmConfigSnapshot:
        if self.fail_reads:
            raise RuntimeError("database message may contain sk-super-secret")
        return self.snapshot

    async def compare_and_swap(
        self,
        *,
        expected_version: int,
        overrides: dict[str, object],
    ) -> RuntimeLlmConfigSnapshot:
        async with self.lock:
            if self.snapshot.version != expected_version:
                raise RuntimeLlmConfigVersionConflict(
                    expected=expected_version,
                    current=self.snapshot.version,
                )
            self.snapshot = RuntimeLlmConfigSnapshot(
                version=expected_version + 1,
                overrides=normalize_runtime_llm_overrides(overrides),
            )
            return self.snapshot

    async def replay_idempotent_result(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> RuntimeLlmConfigSnapshot | None:
        existing = self.idempotency.get(idempotency_key)
        if existing is None:
            return None
        if existing[0] != request_hash:
            raise RuntimeLlmConfigIdempotencyConflict
        return existing[1]

    async def compare_and_swap_idempotent(
        self,
        *,
        expected_version: int,
        overrides: dict[str, object],
        idempotency_key: str,
        request_hash: str,
    ) -> RuntimeLlmConfigMutation:
        async with self.lock:
            replay = await self.replay_idempotent_result(
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return RuntimeLlmConfigMutation(replay, replay, True)
            if self.snapshot.version != expected_version:
                raise RuntimeLlmConfigVersionConflict(
                    expected=expected_version,
                    current=self.snapshot.version,
                )
            before = self.snapshot
            self.snapshot = RuntimeLlmConfigSnapshot(
                version=expected_version + 1,
                overrides=normalize_runtime_llm_overrides(overrides),
            )
            self.idempotency[idempotency_key] = (request_hash, self.snapshot)
            return RuntimeLlmConfigMutation(before, self.snapshot, False)


class _AuditSink:
    def __init__(self) -> None:
        self.events: list[AdminAuditEvent] = []

    async def write(self, event: AdminAuditEvent) -> None:
        self.events.append(event)


def _settings(**updates: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "admin_bearer_token": "runtime-config-admin-token",
        "outbound_hmac_secret": "test-outbound-secret",
        "tenant_demo_secret": "test-tenant-secret",
        "knowledge_features_enabled": False,
        "llm_provider": "openai",
        "llm_embed_provider": "fake",
        "openai_api_key": "PRIVATE_SENTINEL_API_KEY",
    }
    values.update(updates)
    return Settings(**values)


def _app(
    settings: Settings,
    store: _MemoryRuntimeLlmStore,
    *,
    audit_sink: _AuditSink | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        build_admin_router(
            None,
            None,
            settings,
            runtime_llm_config_store=store,  # type: ignore[arg-type]
        )
    )
    if audit_sink is not None:
        install_admin_audit_middleware(app, settings, sink=audit_sink)
    return app


def _headers(**extra: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer runtime-config-admin-token",
        **extra,
    }


@pytest.mark.asyncio
async def test_get_is_versioned_no_store_and_never_echoes_secret() -> None:
    settings = _settings()
    transport = httpx.ASGITransport(app=_app(settings, _MemoryRuntimeLlmStore()))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/admin/runtime/llm-config", headers=_headers())

    assert response.status_code == 200
    assert response.headers["etag"] == '"runtime-llm-config-0"'
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["secret_provider_status"]["openai_api_key"] == {
        "configured": True,
        "source": "dotenv_or_explicit",
        "mutable": False,
    }
    assert "PRIVATE_SENTINEL_API_KEY" not in response.text
    assert "settings_file" not in response.json()


@pytest.mark.asyncio
async def test_post_requires_if_match_and_rejects_secret_fields_in_every_environment() -> None:
    for app_env in ("test", "prod"):
        store = _MemoryRuntimeLlmStore()
        settings = _settings(
            app_env=app_env,
            admin_session_cookie_secure=app_env == "prod",
            outbound_hmac_secret="prod-outbound-secret" if app_env == "prod" else "test-outbound-secret",
            admin_bearer_token="runtime-config-admin-token",
            tenant_demo_secret="prod-tenant-secret" if app_env == "prod" else "test-tenant-secret",
            wxbot_api_token="prod-wxbot-secret" if app_env == "prod" else "",
            orchestrator_flow_runtime_enabled=app_env == "prod",
            orchestrator_flow_runtime_name="auto" if app_env == "prod" else "default_compatible_flow",
            orchestrator_flow_runtime_allowed_names="auto" if app_env == "prod" else "default_compatible_flow",
            orchestrator_flow_runtime_allow_target_flows=app_env == "prod",
            orchestrator_flow_effect_commit_backend="redis" if app_env == "prod" else "none",
            orchestrator_flow_effect_handlers_enabled=app_env == "prod",
            orchestrator_flow_effect_log_backend="postgres" if app_env == "prod" else "none",
            orchestrator_flow_effect_log_failure_policy="fail_closed",
        )
        transport = httpx.ASGITransport(app=_app(settings, store))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            missing = await client.post(
                "/v1/admin/runtime/llm-config",
                headers=_headers(),
                json={"customer_service_prompt_enabled": False},
            )
            missing_idempotency = await client.post(
                "/v1/admin/runtime/llm-config",
                headers=_headers(**{"If-Match": '"runtime-llm-config-0"'}),
                json={"customer_service_prompt_enabled": False},
            )
            secret_write = await client.post(
                "/v1/admin/runtime/llm-config",
                headers=_headers(
                    **{
                        "If-Match": '"runtime-llm-config-0"',
                        "Idempotency-Key": "secret-write",
                    }
                ),
                json={"openai_api_key": "sk-attacker-value"},
            )
            secret_clear = await client.post(
                "/v1/admin/runtime/llm-config",
                headers=_headers(
                    **{
                        "If-Match": '"runtime-llm-config-0"',
                        "Idempotency-Key": "secret-clear",
                    }
                ),
                json={"clear_openai_api_key": True},
            )
            disguised_secret = await client.post(
                "/v1/admin/runtime/llm-config",
                headers=_headers(
                    **{
                        "If-Match": '"runtime-llm-config-0"',
                        "Idempotency-Key": "disguised-secret",
                    }
                ),
                json={"openai_api_mode": "sk-disguised-secret"},
            )

        assert missing.status_code == 428
        assert missing.json()["detail"] == "if_match_required"
        assert missing_idempotency.status_code == 400
        assert missing_idempotency.json()["detail"] == "idempotency_key_required"
        assert secret_write.status_code == 400
        assert secret_write.json()["detail"] == "secret_fields_not_mutable"
        assert secret_clear.status_code == 400
        assert secret_clear.json()["detail"] == "secret_fields_not_mutable"
        assert disguised_secret.status_code == 400
        assert "sk-disguised-secret" not in disguised_secret.text
        assert store.snapshot.version == 0
        assert "sk-attacker-value" not in secret_write.text


@pytest.mark.asyncio
async def test_conditional_write_conflict_and_concurrent_winner_contract() -> None:
    store = _MemoryRuntimeLlmStore()
    transport = httpx.ASGITransport(app=_app(_settings(), store))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        requests = [
            client.post(
                "/v1/admin/runtime/llm-config",
                headers=_headers(
                    **{
                        "If-Match": '"runtime-llm-config-0"',
                        "Idempotency-Key": f"concurrent-{value}",
                    }
                ),
                json={"customer_service_prompt_enabled": value},
            )
            for value in (False, True)
        ]
        first, second = await asyncio.gather(*requests)
        refreshed = await client.get(
            "/v1/admin/runtime/llm-config",
            headers=_headers(),
        )

    responses = sorted((first, second), key=lambda item: item.status_code)
    success, conflict = responses
    assert success.status_code == 200
    assert success.headers["etag"] == '"runtime-llm-config-1"'
    assert success.headers["cache-control"] == "no-store"
    assert success.json()["restart_required"] is True
    assert success.json()["affected_roles"] == ["api", "inbound", "scheduler"]
    assert conflict.status_code == 409
    assert conflict.headers["etag"] == '"runtime-llm-config-1"'
    assert conflict.json()["detail"] == {
        "code": "version_conflict",
        "expected_version": 0,
        "current_version": 1,
    }
    assert refreshed.json()["restart_required"] is True
    assert refreshed.json()["apply_status"] == "restart_required_or_unverified"


@pytest.mark.asyncio
async def test_idempotency_exactly_replays_a_lost_response_and_rejects_key_reuse() -> None:
    store = _MemoryRuntimeLlmStore()
    transport = httpx.ASGITransport(app=_app(_settings(), store))
    headers = _headers(
        **{
            "If-Match": '"runtime-llm-config-0"',
            "Idempotency-Key": "lost-response-intent",
        }
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/v1/admin/runtime/llm-config",
            headers=headers,
            json={"customer_service_prompt_enabled": False},
        )
        replay = await client.post(
            "/v1/admin/runtime/llm-config",
            headers=headers,
            json={"customer_service_prompt_enabled": False},
        )
        conflicting_reuse = await client.post(
            "/v1/admin/runtime/llm-config",
            headers=headers,
            json={"customer_service_prompt_enabled": True},
        )

    assert first.status_code == replay.status_code == 200
    assert first.headers["etag"] == replay.headers["etag"] == '"runtime-llm-config-1"'
    assert first.json() == replay.json()
    assert store.snapshot.version == 1
    assert conflicting_reuse.status_code == 409
    assert conflicting_reuse.headers["etag"] == '"runtime-llm-config-1"'
    assert conflicting_reuse.json()["detail"] == {"code": "idempotency_conflict"}


@pytest.mark.asyncio
async def test_concurrent_same_key_same_payload_commits_once_and_replays() -> None:
    store = _MemoryRuntimeLlmStore()
    transport = httpx.ASGITransport(app=_app(_settings(), store))
    headers = _headers(
        **{
            "If-Match": '"runtime-llm-config-0"',
            "Idempotency-Key": "concurrent-same-intent",
        }
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first, second = await asyncio.gather(
            client.post(
                "/v1/admin/runtime/llm-config",
                headers=headers,
                json={"customer_service_prompt_enabled": False},
            ),
            client.post(
                "/v1/admin/runtime/llm-config",
                headers=headers,
                json={"customer_service_prompt_enabled": False},
            ),
        )

    assert first.status_code == second.status_code == 200
    assert first.headers["etag"] == second.headers["etag"] == '"runtime-llm-config-1"'
    assert first.json() == second.json()
    assert store.snapshot.version == 1


@pytest.mark.asyncio
async def test_read_failure_is_safe_and_does_not_return_local_defaults() -> None:
    transport = httpx.ASGITransport(
        app=_app(_settings(), _MemoryRuntimeLlmStore(fail_reads=True))
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/admin/runtime/llm-config", headers=_headers())

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "runtime_llm_config_unavailable"}
    assert "sk-" not in response.text
    assert "llm_provider" not in response.text


@pytest.mark.asyncio
async def test_api_can_override_an_explicit_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MODEL_TIER1", "environment-model")
    store = _MemoryRuntimeLlmStore()
    transport = httpx.ASGITransport(app=_app(_settings(), store))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/admin/runtime/llm-config",
            headers=_headers(
                **{
                    "If-Match": '"runtime-llm-config-0"',
                    "Idempotency-Key": "environment-shadow",
                }
            ),
            json={"llm_model_tier1": "database-model"},
        )

    assert response.status_code == 200
    assert response.headers["etag"] == '"runtime-llm-config-1"'
    assert response.json()["llm_model_tier1"] == "database-model"
    assert response.json()["field_sources"]["llm_model_tier1"] == "persisted_override"
    assert store.snapshot.version == 1


@pytest.mark.asyncio
async def test_semantic_audit_is_versioned_traced_and_redacted() -> None:
    store = _MemoryRuntimeLlmStore()
    sink = _AuditSink()
    transport = httpx.ASGITransport(app=_app(_settings(), store, audit_sink=sink))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/admin/runtime/llm-config",
            headers=_headers(
                **{
                    "If-Match": '"runtime-llm-config-0"',
                    "Idempotency-Key": "audit-runtime-llm-1",
                    "X-Trace-ID": "trace-runtime-llm-1",
                }
            ),
            json={"customer_service_prompt_enabled": False},
        )

    assert response.status_code == 200
    event = sink.events[-1]
    assert event.target_type == "runtime_llm_config"
    assert event.policy_version == 1
    assert event.trace_id == "trace-runtime-llm-1"
    assert event.reason == "conditional_runtime_llm_config_update"
    assert event.before_state == {
        "version": 0,
        "override_fields": [],
        "changed_fields": [],
        "knowledge_features_enabled": False,
        "customer_service_prompt_enabled": True,
        "openai_web_search_enabled": False,
    }
    assert event.after_state is not None
    assert event.after_state["changed_fields"] == ["customer_service_prompt_enabled"]
    assert "sk-" not in json.dumps(event.as_dict())


def test_database_override_wins_over_environment_and_role_boundary_is_narrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MODEL_TIER1", "environment-model")
    settings = _settings()
    resolved = resolve_runtime_llm_config(
        settings,
        RuntimeLlmConfigSnapshot(
            version=4,
            overrides={"llm_model_tier1": "persisted-model"},
        ),
    )

    assert resolved.settings.llm_model_tier1 == "persisted-model"
    assert resolved.field_sources["llm_model_tier1"] == "persisted_override"
    assert all(runtime_llm_overlay_enabled_for_role(role) for role in ("api", "inbound", "scheduler"))
    assert not runtime_llm_overlay_enabled_for_role("outbound")
    assert not runtime_llm_overlay_enabled_for_role("wxbot_bridge")


def test_persisted_override_wins_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_MODEL_TIER2", raising=False)
    settings = _settings(llm_model_tier2="default-model")
    resolved = resolve_runtime_llm_config(
        settings,
        RuntimeLlmConfigSnapshot(
            version=2,
            overrides={"llm_model_tier2": "persisted-model"},
        ),
    )

    assert resolved.settings.llm_model_tier2 == "persisted-model"
    assert resolved.field_sources["llm_model_tier2"] == "persisted_override"


def test_secret_provider_wins_over_persisted_override(tmp_path: Path) -> None:
    secret_dir = tmp_path / "runtime-secrets"
    secret_dir.mkdir()
    (secret_dir / "llm_model_tier3").write_text("secret-provider-model", encoding="utf-8")

    class SecretBackedSettings(Settings):
        pass

    SecretBackedSettings.model_config = {
        **Settings.model_config,
        "env_file": None,
        "secrets_dir": str(secret_dir),
    }

    settings = SecretBackedSettings()
    resolved = resolve_runtime_llm_config(
        settings,
        RuntimeLlmConfigSnapshot(
            version=8,
            overrides={"llm_model_tier3": "persisted-model"},
        ),
    )

    assert settings.llm_model_tier3 == "secret-provider-model"
    assert resolved.settings.llm_model_tier3 == "secret-provider-model"
    assert resolved.field_sources["llm_model_tier3"] == "secret_provider"


def test_persisted_override_wins_over_dotenv(tmp_path: Path) -> None:
    dotenv_path = tmp_path / "runtime.env"
    dotenv_path.write_text("LLM_EMBED_MODEL=dotenv-model\n", encoding="utf-8")

    class DotenvSettings(Settings):
        pass

    DotenvSettings.model_config = {
        **Settings.model_config,
        "env_file": str(dotenv_path),
        "secrets_dir": None,
    }

    settings = DotenvSettings()
    resolved = resolve_runtime_llm_config(
        settings,
        RuntimeLlmConfigSnapshot(
            version=9,
            overrides={"llm_embed_model": "persisted-model"},
        ),
    )

    assert settings.llm_embed_model == "dotenv-model"
    assert resolved.settings.llm_embed_model == "persisted-model"
    assert resolved.field_sources["llm_embed_model"] == "persisted_override"


@pytest.mark.asyncio
async def test_database_store_performs_atomic_compare_and_swap(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runtime-llm.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(runtime_llm_config_table.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    first_process = RuntimeLlmConfigStore(factory)
    second_process = RuntimeLlmConfigStore(factory)
    updates = {"llm_model_tier1": "model-a"}
    request_hash = runtime_llm_request_hash(expected_version=0, updates=updates)

    try:
        written = await first_process.compare_and_swap_idempotent(
            expected_version=0,
            overrides=updates,
            idempotency_key="database-lost-response",
            request_hash=request_hash,
        )
        assert written.after.version == 1
        assert written.replayed is False
        replayed = await second_process.compare_and_swap_idempotent(
            expected_version=0,
            overrides=updates,
            idempotency_key="database-lost-response",
            request_hash=request_hash,
        )
        assert replayed.replayed is True
        assert replayed.after == written.after
        assert (await second_process.get()).version == 1

        different_hash = runtime_llm_request_hash(
            expected_version=0,
            updates={"llm_model_tier1": "model-b"},
        )
        with pytest.raises(RuntimeLlmConfigIdempotencyConflict):
            await second_process.compare_and_swap_idempotent(
                expected_version=0,
                overrides={"llm_model_tier1": "model-b"},
                idempotency_key="database-lost-response",
                request_hash=different_hash,
            )
        with pytest.raises(RuntimeLlmConfigVersionConflict) as conflict:
            await second_process.compare_and_swap_idempotent(
                expected_version=0,
                overrides={"llm_model_tier1": "model-b"},
                idempotency_key="different-intent",
                request_hash=different_hash,
            )
        assert conflict.value.current == 1
        assert (await second_process.get()).overrides == {"llm_model_tier1": "model-a"}
        async with factory() as session:
            idempotency_rows = (
                await session.execute(select(runtime_llm_config_idempotency_table))
            ).mappings().all()
            history_rows = (
                await session.execute(select(runtime_llm_config_history_table))
            ).mappings().all()
        assert len(idempotency_rows) == len(history_rows) == 1
        assert idempotency_rows[0]["key_hash"] != "database-lost-response"
        assert set(idempotency_rows[0]) == {
            "key_hash",
            "request_hash",
            "result_version",
            "created_at",
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bootstrap_loader_excludes_non_llm_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as main_module

    loaded_roles: list[str] = []

    async def fake_load(settings: Settings):
        loaded_roles.append(settings.app_process_role)
        return type(
            "ResolvedRuntimeConfig",
            (),
            {"settings": settings.model_copy(update={"llm_model_tier1": "db-model"})},
        )()

    monkeypatch.setattr(main_module, "load_runtime_llm_config", fake_load)

    for role in ("outbound", "wxbot_bridge"):
        settings = _settings(app_process_role=role)
        assert await main_module._load_runtime_llm_settings_for_role(settings) is settings

    for role in ("api", "inbound", "scheduler"):
        settings = _settings(app_process_role=role)
        resolved = await main_module._load_runtime_llm_settings_for_role(settings)
        assert resolved.llm_model_tier1 == "db-model"

    assert loaded_roles == ["api", "inbound", "scheduler"]


def test_migration_is_unique_successor_and_has_no_secret_column() -> None:
    path = Path("migrations/versions/20260718_0025_runtime_llm_config.py")
    spec = importlib.util.spec_from_file_location("migration_0025_runtime_llm", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "0025_runtime_llm_config"
    assert module.down_revision == "0024_reply_policy_atomicity"
    source = path.read_text(encoding="utf-8").lower()
    assert "openai_api_key" not in source

    idempotency_path = Path(
        "migrations/versions/20260718_0027_runtime_llm_idempotency.py"
    )
    idempotency_spec = importlib.util.spec_from_file_location(
        "migration_0027_runtime_llm_idempotency",
        idempotency_path,
    )
    assert idempotency_spec is not None and idempotency_spec.loader is not None
    idempotency_module = importlib.util.module_from_spec(idempotency_spec)
    idempotency_spec.loader.exec_module(idempotency_module)
    assert idempotency_module.revision == "0027_runtime_llm_idempotency"
    assert idempotency_module.down_revision == "0026_plugin_admin_idempotency"
    idempotency_source = idempotency_path.read_text(encoding="utf-8").lower()
    assert "response_json" not in idempotency_source
    assert "openai_api_key" not in idempotency_source
