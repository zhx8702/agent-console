from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.common.config import Settings
from app.infra.runtime_schema import (
    RUNTIME_SCHEMA_COMPATIBILITY_LEVEL,
    RUNTIME_SCHEMA_REVISION,
    RuntimeSchemaError,
)
from app.ops import smoke as smoke_module
from app.ops.smoke import (
    SmokeCheckResult,
    check_admin_auth,
    check_database,
    determine_exit_code,
    dlq_backlog_result,
    expected_migration_heads,
)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "app_env": "test",
        "admin_bearer_token": "smoke-admin-token",
        "admin_session_cookie_name": "agent_console_admin_session",
        "admin_session_cookie_secure": False,
    }
    values.update(overrides)
    return Settings(**values)


class _FakeResult:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)


class _FakeConnection:
    def __init__(self, heads: tuple[str, ...]) -> None:
        self._heads = heads
        self.statements: list[str] = []

    async def execute(self, statement: object) -> _FakeResult:
        rendered = str(statement)
        self.statements.append(rendered)
        if "FROM alembic_version" in rendered:
            return _FakeResult([(head,) for head in self._heads])
        return _FakeResult([])


class _FakeConnectionContext:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self._connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeEngine:
    def __init__(self, heads: tuple[str, ...]) -> None:
        self.connection = _FakeConnection(heads)
        self.disposed = False

    def connect(self) -> _FakeConnectionContext:
        return _FakeConnectionContext(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


def _install_fake_database(
    monkeypatch: pytest.MonkeyPatch,
    *,
    current_heads: tuple[str, ...],
    expected_heads: tuple[str, ...] = (RUNTIME_SCHEMA_REVISION,),
) -> _FakeEngine:
    engine = _FakeEngine(current_heads)
    monkeypatch.setattr(
        smoke_module,
        "create_async_engine",
        lambda *_args, **_kwargs: engine,
    )
    monkeypatch.setattr(
        smoke_module,
        "expected_migration_heads",
        lambda _project_root: expected_heads,
    )
    return engine


def test_expected_migration_heads_reads_repo() -> None:
    project_root = Path(__file__).resolve().parents[2]
    heads = expected_migration_heads(project_root)
    assert heads == (RUNTIME_SCHEMA_REVISION,)


@pytest.mark.asyncio
async def test_admin_smoke_uses_session_when_bearer_fallback_is_disabled() -> None:
    settings = _settings(admin_allow_bearer_fallback=False)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/v1/admin/auth/session":
            return httpx.Response(
                200,
                headers={
                    "Set-Cookie": (
                        "agent_console_admin_session=signed-session; "
                        "Path=/; HttpOnly; SameSite=Strict"
                    )
                },
                json={"authenticated": True, "expires_at": 1234567890},
            )
        if request.method == "GET" and request.url.path == "/v1/admin/dlq/messages":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(404, json={"detail": "not_found"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://smoke.test",
    ) as client:
        result = await check_admin_auth(client, settings, tenant_id="tenant-a")

    assert settings.admin_allow_bearer_fallback is False
    assert result == SmokeCheckResult(
        "admin.auth",
        "ok",
        "session_dlq_admin_access_ok",
    )
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/v1/admin/auth/session"),
        ("GET", "/v1/admin/dlq/messages"),
    ]
    assert requests[0].headers["authorization"] == "Bearer smoke-admin-token"
    assert "authorization" not in requests[1].headers
    assert requests[1].headers["cookie"] == (
        "agent_console_admin_session=signed-session"
    )
    assert requests[1].url.params["tenant_id"] == "tenant-a"


@pytest.mark.asyncio
async def test_database_smoke_rejects_missing_alembic_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _install_fake_database(monkeypatch, current_heads=())

    result = await check_database(_settings())

    assert result == SmokeCheckResult(
        "db.migration",
        "fail",
        "alembic_version_missing",
    )
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_database_smoke_reads_all_rows_and_rejects_double_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _install_fake_database(
        monkeypatch,
        current_heads=("head-a", "head-b"),
        expected_heads=("head-b",),
    )

    result = await check_database(_settings())

    assert result == SmokeCheckResult(
        "db.migration",
        "fail",
        "alembic_multiple_heads current=head-a,head-b",
    )
    version_queries = [
        statement
        for statement in engine.connection.statements
        if "FROM alembic_version" in statement
    ]
    assert len(version_queries) == 1
    assert "ORDER BY version_num" in version_queries[0]
    assert "LIMIT" not in version_queries[0].upper()


@pytest.mark.asyncio
async def test_database_smoke_rejects_unexpected_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_database(
        monkeypatch,
        current_heads=("old-head",),
        expected_heads=("new-head",),
    )

    result = await check_database(_settings())

    assert result == SmokeCheckResult(
        "db.migration",
        "fail",
        "alembic_head_mismatch current=old-head expected=new-head",
    )


@pytest.mark.asyncio
async def test_database_smoke_rejects_runtime_contract_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _install_fake_database(
        monkeypatch,
        current_heads=(RUNTIME_SCHEMA_REVISION,),
    )

    async def reject_contract(_engine: object, *, component: str) -> None:
        assert _engine is engine
        assert component == "production smoke"
        raise RuntimeSchemaError("compatibility level drifted")

    monkeypatch.setattr(smoke_module, "verify_runtime_schema", reject_contract)

    result = await check_database(_settings())

    assert result.status == "fail"
    assert result.detail.startswith(
        "runtime_schema_contract_invalid contract=agent-console-runtime "
        f"compatibility={RUNTIME_SCHEMA_COMPATIBILITY_LEVEL}"
    )
    assert "compatibility level drifted" in result.detail
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_database_smoke_accepts_exact_head_and_runtime_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _install_fake_database(
        monkeypatch,
        current_heads=(RUNTIME_SCHEMA_REVISION,),
    )
    verified: list[str] = []

    async def accept_contract(_engine: object, *, component: str) -> None:
        assert _engine is engine
        verified.append(component)

    monkeypatch.setattr(smoke_module, "verify_runtime_schema", accept_contract)

    result = await check_database(_settings())

    assert result == SmokeCheckResult(
        "db.migration",
        "ok",
        f"current={RUNTIME_SCHEMA_REVISION} "
        "contract=agent-console-runtime "
        f"compatibility={RUNTIME_SCHEMA_COMPATIBILITY_LEVEL}",
    )
    assert verified == ["production smoke"]
    assert engine.disposed is True


def test_dlq_backlog_warns_by_default() -> None:
    result = dlq_backlog_result(dlq_length=3, fail_on_dlq=False)

    assert result.name == "redis.dlq"
    assert result.status == "warn"
    assert result.detail == "backlog=3"


def test_dlq_backlog_can_fail() -> None:
    result = dlq_backlog_result(dlq_length=2, fail_on_dlq=True)

    assert result.status == "fail"


def test_determine_exit_code_ignores_warnings() -> None:
    results = [
        SmokeCheckResult("api.healthz", "ok", "status=ok"),
        SmokeCheckResult("redis.dlq", "warn", "backlog=1"),
    ]

    assert determine_exit_code(results) == 0


def test_determine_exit_code_fails_on_failures() -> None:
    results = [
        SmokeCheckResult("api.healthz", "ok", "status=ok"),
        SmokeCheckResult("db.migration", "fail", "current=old expected=new"),
    ]

    assert determine_exit_code(results) == 1
