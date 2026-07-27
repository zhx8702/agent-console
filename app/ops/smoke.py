from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from redis.asyncio import from_url as redis_from_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.common.config import Settings, get_settings
from app.egress.safe_http import safe_trusted_service_request
from app.infra.runtime_schema import (
    RUNTIME_SCHEMA_COMPATIBILITY_LEVEL,
    RUNTIME_SCHEMA_CONTRACT_NAME,
    RuntimeSchemaError,
    verify_runtime_schema,
)

CheckStatus = Literal["ok", "warn", "fail"]

_ADMIN_SESSION_PATH = "/v1/admin/auth/session"
_ADMIN_PROBE_PATH = "/v1/admin/dlq/messages"
_ADMIN_RESPONSE_CONTENT_TYPES = (
    "application/json",
    "application/problem+json",
    "text/plain",
)


@dataclass
class SmokeCheckResult:
    name: str
    status: CheckStatus
    detail: str


def expected_migration_heads(project_root: Path) -> tuple[str, ...]:
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    script = ScriptDirectory.from_config(config)
    return tuple(script.get_heads())


def determine_exit_code(results: list[SmokeCheckResult]) -> int:
    return 1 if any(result.status == "fail" for result in results) else 0


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _format_result(result: SmokeCheckResult) -> str:
    return f"[{result.status.upper()}] {result.name}: {result.detail}"


def dlq_backlog_result(*, dlq_length: int, fail_on_dlq: bool) -> SmokeCheckResult:
    if dlq_length == 0:
        return SmokeCheckResult("redis.dlq", "ok", "backlog=0")
    status: CheckStatus = "fail" if fail_on_dlq else "warn"
    return SmokeCheckResult("redis.dlq", status, f"backlog={dlq_length}")


async def check_api_health(client: httpx.AsyncClient) -> SmokeCheckResult:
    try:
        resp = await safe_trusted_service_request(
            client,
            "GET",
            str(client.base_url),
            "/healthz",
            headers={"Accept": "application/json"},
            timeout_seconds=5.0,
            max_response_bytes=1024 * 1024,
            allowed_response_content_types=(
                "application/json",
                "application/problem+json",
                "text/plain",
            ),
        )
    except Exception as exc:
        return SmokeCheckResult("api.healthz", "fail", f"request_failed:{exc.__class__.__name__}")
    if resp.status_code != 200:
        return SmokeCheckResult("api.healthz", "fail", f"status={resp.status_code}")
    payload = resp.json()
    if payload.get("status") != "ok":
        return SmokeCheckResult("api.healthz", "fail", f"payload={payload}")
    return SmokeCheckResult("api.healthz", "ok", "status=ok")


async def check_api_ready(client: httpx.AsyncClient) -> SmokeCheckResult:
    try:
        resp = await safe_trusted_service_request(
            client,
            "GET",
            str(client.base_url),
            "/readyz",
            headers={"Accept": "application/json"},
            timeout_seconds=5.0,
            max_response_bytes=1024 * 1024,
            allowed_response_content_types=(
                "application/json",
                "application/problem+json",
                "text/plain",
            ),
        )
    except Exception as exc:
        return SmokeCheckResult("api.readyz", "fail", f"request_failed:{exc.__class__.__name__}")
    try:
        payload = resp.json()
    except Exception:
        payload = {}
    if resp.status_code != 200 or payload.get("status") != "ready":
        errors = payload.get("errors") if isinstance(payload, dict) else None
        return SmokeCheckResult("api.readyz", "fail", f"status={resp.status_code} errors={errors}")
    return SmokeCheckResult("api.readyz", "ok", "status=ready")


async def check_admin_auth(
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    tenant_id: str,
) -> SmokeCheckResult:
    if not settings.admin_bearer_token:
        return SmokeCheckResult("admin.auth", "fail", "admin_bearer_token_missing")

    # Production disables bearer fallback on ordinary admin endpoints.  The
    # official session exchange remains the deliberate compatibility boundary:
    # it authenticates the bootstrap bearer once and issues a short-lived
    # HttpOnly cookie for subsequent control-plane requests.
    try:
        session_response = await safe_trusted_service_request(
            client,
            "POST",
            str(client.base_url),
            _ADMIN_SESSION_PATH,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {settings.admin_bearer_token}",
            },
            timeout_seconds=5.0,
            max_response_bytes=2 * 1024 * 1024,
            allowed_response_content_types=_ADMIN_RESPONSE_CONTENT_TYPES,
        )
    except Exception as exc:
        return SmokeCheckResult(
            "admin.auth",
            "fail",
            f"admin_session_request_failed:{exc.__class__.__name__}",
        )
    if session_response.status_code != 200:
        return SmokeCheckResult(
            "admin.auth",
            "fail",
            f"admin_session_status={session_response.status_code}",
        )

    cookie_name = str(settings.admin_session_cookie_name or "").strip()
    if not cookie_name:
        return SmokeCheckResult("admin.auth", "fail", "admin_session_cookie_name_missing")

    # safe_trusted_service_request intentionally bypasses the client's cookie
    # jar while pinning the destination.  Extract into an isolated jar, then
    # render the Cookie header for the exact probe URL so Secure/Domain/Path
    # attributes are still enforced instead of copying Set-Cookie verbatim.
    session_cookies = httpx.Cookies()
    try:
        session_cookies.extract_cookies(session_response)
        cookie_value = session_cookies.get(cookie_name)
    except httpx.CookieConflict:
        return SmokeCheckResult("admin.auth", "fail", "admin_session_cookie_ambiguous")
    if not cookie_value:
        return SmokeCheckResult("admin.auth", "fail", "admin_session_cookie_missing")

    probe_url = f"{str(client.base_url).rstrip('/')}{_ADMIN_PROBE_PATH}"
    cookie_request = httpx.Request("GET", probe_url)
    session_cookies.set_cookie_header(cookie_request)
    cookie_header = str(cookie_request.headers.get("Cookie") or "").strip()
    if not cookie_header:
        return SmokeCheckResult("admin.auth", "fail", "admin_session_cookie_unusable")

    try:
        probe_response = await safe_trusted_service_request(
            client,
            "GET",
            str(client.base_url),
            _ADMIN_PROBE_PATH,
            params={"tenant_id": tenant_id, "limit": 1},
            headers={
                "Accept": "application/json",
                "Cookie": cookie_header,
            },
            timeout_seconds=5.0,
            max_response_bytes=2 * 1024 * 1024,
            allowed_response_content_types=_ADMIN_RESPONSE_CONTENT_TYPES,
        )
    except Exception as exc:
        return SmokeCheckResult(
            "admin.auth",
            "fail",
            f"admin_probe_request_failed:{exc.__class__.__name__}",
        )
    if probe_response.status_code != 200:
        return SmokeCheckResult(
            "admin.auth",
            "fail",
            f"admin_probe_status={probe_response.status_code}",
        )
    return SmokeCheckResult("admin.auth", "ok", "session_dlq_admin_access_ok")


def _migration_head_result(
    *,
    current_heads: tuple[str, ...],
    expected_heads: tuple[str, ...],
) -> SmokeCheckResult:
    if not current_heads:
        return SmokeCheckResult("db.migration", "fail", "alembic_version_missing")
    if len(current_heads) != 1:
        return SmokeCheckResult(
            "db.migration",
            "fail",
            f"alembic_multiple_heads current={','.join(current_heads)}",
        )
    if len(expected_heads) != 1:
        expected = ",".join(expected_heads) or "missing"
        return SmokeCheckResult(
            "db.migration",
            "fail",
            f"alembic_repository_heads_invalid expected={expected}",
        )
    if current_heads[0] != expected_heads[0]:
        return SmokeCheckResult(
            "db.migration",
            "fail",
            f"alembic_head_mismatch current={current_heads[0]} expected={expected_heads[0]}",
        )
    return SmokeCheckResult("db.migration", "ok", f"current={current_heads[0]}")


async def check_database(settings: Settings) -> SmokeCheckResult:
    engine = create_async_engine(settings.db_dsn, future=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            revision_result = await conn.execute(
                text("SELECT version_num FROM alembic_version ORDER BY version_num")
            )
            current_heads = tuple(
                str(row[0]) if row[0] is not None else "<null>"
                for row in revision_result.fetchall()
            )

        expected_heads = expected_migration_heads(settings.project_root)
        head_result = _migration_head_result(
            current_heads=current_heads,
            expected_heads=expected_heads,
        )
        if head_result.status == "fail":
            return head_result

        try:
            await verify_runtime_schema(engine, component="production smoke")
        except RuntimeSchemaError as exc:
            detail = " ".join(str(exc).split())
            return SmokeCheckResult(
                "db.migration",
                "fail",
                "runtime_schema_contract_invalid "
                f"contract={RUNTIME_SCHEMA_CONTRACT_NAME} "
                f"compatibility={RUNTIME_SCHEMA_COMPATIBILITY_LEVEL} detail={detail}",
            )
    except Exception as exc:
        return SmokeCheckResult("db.migration", "fail", f"db_check_failed:{exc.__class__.__name__}")
    finally:
        await engine.dispose()

    return SmokeCheckResult(
        "db.migration",
        "ok",
        f"current={current_heads[0]} "
        f"contract={RUNTIME_SCHEMA_CONTRACT_NAME} "
        f"compatibility={RUNTIME_SCHEMA_COMPATIBILITY_LEVEL}",
    )


async def check_redis(settings: Settings, *, fail_on_dlq: bool) -> list[SmokeCheckResult]:
    redis = redis_from_url(settings.redis_url, decode_responses=True, encoding="utf-8")
    try:
        await redis.ping()
        inbound_len, outbound_len, dlq_len = await asyncio.gather(
            redis.xlen(settings.bus_inbound_stream),
            redis.xlen(settings.bus_outbound_stream),
            redis.xlen(settings.bus_dlq_stream),
        )
    except Exception as exc:
        return [SmokeCheckResult("redis", "fail", f"redis_check_failed:{exc.__class__.__name__}")]
    finally:
        await redis.aclose()

    return [
        SmokeCheckResult(
            "redis",
            "ok",
            f"inbound={inbound_len} outbound={outbound_len} dlq={dlq_len}",
        ),
        dlq_backlog_result(dlq_length=dlq_len, fail_on_dlq=fail_on_dlq),
    ]


async def check_qdrant(settings: Settings) -> SmokeCheckResult:
    if not settings.knowledge_features_enabled:
        return SmokeCheckResult("qdrant", "ok", "skipped_knowledge_disabled")
    try:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
            resp = await safe_trusted_service_request(
                client,
                "GET",
                settings.qdrant_url.rstrip("/"),
                "/healthz",
                headers={"Accept": "application/json, text/plain"},
                timeout_seconds=3.0,
                max_response_bytes=1024 * 1024,
                allowed_response_content_types=(
                    "application/json",
                    "application/problem+json",
                    "text/plain",
                ),
            )
    except Exception as exc:
        return SmokeCheckResult("qdrant", "fail", f"request_failed:{exc.__class__.__name__}")
    if resp.status_code >= 500:
        return SmokeCheckResult("qdrant", "fail", f"status={resp.status_code}")
    return SmokeCheckResult("qdrant", "ok", f"status={resp.status_code}")


async def run_smoke_checks(
    settings: Settings,
    *,
    base_url: str,
    tenant_id: str,
    fail_on_dlq: bool,
) -> list[SmokeCheckResult]:
    results: list[SmokeCheckResult] = []

    async with httpx.AsyncClient(
        base_url=_normalize_base_url(base_url), timeout=5.0, trust_env=False
    ) as client:
        results.extend(
            await asyncio.gather(
                check_api_health(client),
                check_api_ready(client),
                check_admin_auth(client, settings, tenant_id=tenant_id),
            )
        )

    db_result, qdrant_result = await asyncio.gather(
        check_database(settings),
        check_qdrant(settings),
    )
    redis_results = await check_redis(settings, fail_on_dlq=fail_on_dlq)

    results.append(db_result)
    results.extend(redis_results)
    results.append(qdrant_result)
    return results


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run production smoke checks against cs-system.")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Base URL for the API process. Default: http://127.0.0.1:8000",
    )
    parser.add_argument(
        "--tenant-id",
        default="demo",
        help="Tenant id used for the admin DLQ probe. Default: demo",
    )
    parser.add_argument(
        "--fail-on-dlq",
        action="store_true",
        help="Treat a non-empty DLQ backlog as a failing check.",
    )
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    settings = get_settings()
    results = await run_smoke_checks(
        settings,
        base_url=args.base_url,
        tenant_id=args.tenant_id,
        fail_on_dlq=args.fail_on_dlq,
    )

    for result in results:
        print(_format_result(result))

    fail_count = sum(1 for result in results if result.status == "fail")
    warn_count = sum(1 for result in results if result.status == "warn")
    print(f"Summary: total={len(results)} fail={fail_count} warn={warn_count}")
    return determine_exit_code(results)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
