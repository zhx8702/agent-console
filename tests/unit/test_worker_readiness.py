from __future__ import annotations

import pytest

from app.common.config import Settings
from app.infra.runtime_schema import RuntimeSchemaError
from app.workers.readiness import (
    RoleDependenciesUnavailable,
    ensure_role_dependencies_ready,
    probe_db_semantics,
    probe_role_dependencies,
    probe_wxbot_sdk_semantics,
    required_dependencies_for_role,
)


def _settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "knowledge_features_enabled": True,
        "outbound_hmac_secret": "test_secret",
        "tenant_demo_secret": "test_tenant_secret",
    }
    values.update(updates)
    return Settings(**values)


def test_role_dependency_matrix_isolated_by_runtime_responsibility() -> None:
    settings = _settings()

    assert required_dependencies_for_role("inbound", settings) == (
        "redis",
        "db",
        "qdrant",
    )
    assert required_dependencies_for_role("scheduler", settings) == (
        "redis",
        "db",
        "qdrant",
    )
    assert required_dependencies_for_role("outbound", settings) == (
        "redis",
        "db",
    )
    assert required_dependencies_for_role("wxbot_bridge", settings) == (
        "redis",
        "db",
        "wxbot_sdk",
    )
    assert required_dependencies_for_role("api", settings) == ("redis",)

    production = _settings(app_env="prod")
    assert required_dependencies_for_role("api", production) == (
        "redis",
        "db",
        "qdrant",
    )


@pytest.mark.asyncio
async def test_db_probe_verifies_runtime_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = object()
    calls: list[tuple[object, str]] = []

    async def verify(candidate: object, *, component: str) -> None:
        calls.append((candidate, component))

    monkeypatch.setattr("app.workers.readiness.get_engine", lambda: engine)
    monkeypatch.setattr("app.workers.readiness.verify_runtime_compatibility", verify)

    assert await probe_db_semantics()
    assert calls == [(engine, "worker readiness")]


@pytest.mark.asyncio
async def test_db_probe_rejects_schema_contract_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject(_engine: object, *, component: str) -> None:
        assert component == "worker readiness"
        raise RuntimeSchemaError("compatibility level mismatch")

    monkeypatch.setattr("app.workers.readiness.get_engine", object)
    monkeypatch.setattr("app.workers.readiness.verify_runtime_compatibility", reject)

    assert not await probe_db_semantics()


@pytest.mark.asyncio
async def test_outbound_readiness_never_initializes_or_probes_qdrant() -> None:
    calls: list[str] = []

    async def redis_ok() -> bool:
        calls.append("redis")
        return True

    async def db_ok() -> bool:
        calls.append("db")
        return True

    async def forbidden_qdrant() -> bool:
        raise AssertionError("outbound must not depend on Qdrant")

    result = await probe_role_dependencies(
        "outbound",
        _settings(app_process_role="outbound"),
        redis_probe=redis_ok,
        db_probe=db_ok,
        qdrant_probe=forbidden_qdrant,
    )

    assert result.ready
    assert result.checks == {"redis": True, "db": True}
    assert calls == ["redis", "db"]


@pytest.mark.asyncio
async def test_core_role_never_probes_optional_wxbot_connection() -> None:
    async def ok() -> bool:
        return True

    async def forbidden_sdk() -> bool:
        raise AssertionError("core readiness must not depend on a WeChat connection")

    result = await probe_role_dependencies(
        "inbound",
        _settings(knowledge_features_enabled=False),
        redis_probe=ok,
        db_probe=ok,
        wxbot_sdk_probe=forbidden_sdk,
    )

    assert result.ready
    assert result.required == ("redis", "db")


@pytest.mark.asyncio
async def test_wxbot_worker_readiness_requires_authenticated_sdk_identity() -> None:
    async def ok() -> bool:
        return True

    async def sdk_not_authenticated() -> bool:
        return False

    result = await probe_role_dependencies(
        "wxbot_bridge",
        _settings(knowledge_features_enabled=False),
        redis_probe=ok,
        db_probe=ok,
        wxbot_sdk_probe=sdk_not_authenticated,
    )

    assert not result.ready
    assert result.checks == {"redis": True, "db": True, "wxbot_sdk": False}
    assert result.errors == ("wxbot_sdk_unreachable",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "status": "running",
                "auth_active": True,
                "identity": {"ready": True, "self_wxid": "wxid_bot"},
            },
            True,
        ),
        (
            {
                "status": "running",
                "auth_active": False,
                "identity": {"ready": True},
            },
            False,
        ),
        (
            {
                "status": "running",
                "auth_active": True,
                "config": {"self_wxid": "wxid_legacy_bot"},
            },
            True,
        ),
        (
            {
                "status": "running",
                "auth_active": True,
                "config": {"self_wxid": ""},
            },
            False,
        ),
        (
            {
                "status": "unhealthy",
                "auth_active": True,
                "identity": {"ready": False},
            },
            False,
        ),
    ],
)
async def test_wxbot_sdk_probe_checks_transport_auth_and_identity(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    expected: bool,
) -> None:
    captured_headers: dict[str, str] = {}

    class _Response:
        status_code = 200

        def json(self) -> dict[str, object]:
            return payload

    async def fake_request(*_args, **kwargs):
        captured_headers.update(kwargs["headers"])
        return _Response()

    monkeypatch.setattr(
        "app.workers.readiness.safe_trusted_service_request",
        fake_request,
    )

    result = await probe_wxbot_sdk_semantics(
        _settings(
            wxbot_sdk_url="http://127.0.0.1:5080",
            wxbot_api_token="sdk-service-token",
        )
    )

    assert result is expected
    assert captured_headers["Authorization"] == "Bearer sdk-service-token"


@pytest.mark.asyncio
async def test_wxbot_sdk_probe_recovers_from_transient_transport_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[float] = []

    class _Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "status": "running",
                "auth_active": True,
                "identity": {"ready": True, "self_wxid": "wxid_bot"},
            }

    async def flaky_request(*_args, **kwargs):
        calls.append(float(kwargs["timeout_seconds"]))
        if len(calls) < 3:
            raise TimeoutError("temporary SDK stall")
        return _Response()

    monkeypatch.setattr(
        "app.workers.readiness.safe_trusted_service_request",
        flaky_request,
    )

    result = await probe_wxbot_sdk_semantics(
        _settings(wxbot_sdk_url="http://127.0.0.1:5080"),
        attempts=3,
        timeout_seconds=5.0,
        retry_delay_seconds=0,
    )

    assert result is True
    assert calls == [5.0, 5.0, 5.0]


@pytest.mark.asyncio
async def test_inbound_readiness_fails_closed_on_semantic_qdrant_failure() -> None:
    async def ok() -> bool:
        return True

    async def qdrant_down() -> bool:
        return False

    result = await probe_role_dependencies(
        "inbound",
        _settings(app_process_role="inbound"),
        redis_probe=ok,
        db_probe=ok,
        qdrant_probe=qdrant_down,
    )

    assert not result.ready
    assert result.errors == ("qdrant_unreachable",)


@pytest.mark.asyncio
async def test_ensure_role_dependencies_prevents_start_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def redis_down(_redis=None) -> bool:
        return False

    async def db_ok() -> bool:
        return True

    monkeypatch.setattr(
        "app.workers.readiness.probe_redis_semantics",
        redis_down,
    )
    monkeypatch.setattr("app.workers.readiness.probe_db_semantics", db_ok)

    with pytest.raises(
        RoleDependenciesUnavailable,
        match="redis_unreachable",
    ):
        await ensure_role_dependencies_ready(
            "outbound",
            _settings(
                app_process_role="outbound",
                knowledge_features_enabled=False,
            ),
            redis=object(),
        )
