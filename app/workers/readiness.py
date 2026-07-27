from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.common.config import Settings
from app.common.wxbot_auth import wxbot_sdk_headers
from app.egress.safe_http import safe_trusted_service_request
from app.infra.db import get_engine
from app.infra.redis_client import get_redis
from app.infra.runtime_schema import verify_runtime_compatibility

PROCESS_ROLES = frozenset({"api", "inbound", "outbound", "scheduler", "wxbot_bridge"})
WORKER_ROLES = frozenset({"inbound", "outbound", "scheduler", "wxbot_bridge"})


class RoleDependenciesUnavailable(RuntimeError):
    """Raised before consumption when a role's semantic dependencies are down."""

    def __init__(self, role: str, errors: tuple[str, ...]) -> None:
        self.role = role
        self.errors = errors
        super().__init__(f"{role} dependencies unavailable: {', '.join(errors)}")


@dataclass(frozen=True, slots=True)
class RoleReadiness:
    role: str
    checks: Mapping[str, bool]
    required: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.errors

    @property
    def detail(self) -> str:
        return "ready" if self.ready else ",".join(self.errors)


def required_dependencies_for_role(role: str, settings: Settings) -> tuple[str, ...]:
    """Return only dependencies whose loss makes this role unsafe to run."""

    normalized = str(role or "").strip().lower()
    if normalized not in PROCESS_ROLES:
        return ()

    required: list[str] = ["redis"]
    # API development can intentionally expose diagnostics while Postgres is
    # offline. Every state-mutating worker, and production API, fails closed.
    if normalized != "api" or settings.is_prod:
        required.append("db")
    # Only inference/scheduled-memory roles use the vector backend. Egress and
    # the SDK bridge must remain independent from Qdrant.
    if (
        settings.knowledge_features_enabled
        and (
            normalized in {"inbound", "scheduler"}
            or (normalized == "api" and settings.is_prod)
        )
    ):
        required.append("qdrant")
    # The bridge is an optional process, but once an operator starts it its
    # own ready signal must mean the configured SDK is authenticated and has a
    # usable self identity.  Core API/worker roles deliberately do not inherit
    # this connection-level dependency.
    if normalized == "wxbot_bridge":
        required.append("wxbot_sdk")
    return tuple(required)


async def probe_redis_semantics(redis: Any | None = None) -> bool:
    """Verify Redis command execution, not merely TCP reachability."""

    try:
        client = redis if redis is not None else get_redis()
        return bool(await client.ping())
    except Exception:
        return False


async def probe_db_semantics() -> bool:
    """Verify database reachability and the worker's schema compatibility."""

    try:
        await verify_runtime_compatibility(
            get_engine(),
            component="worker readiness",
        )
        return True
    except Exception:
        return False


async def probe_qdrant_semantics(settings: Settings) -> bool:
    """Verify Qdrant health, collection listing, and a read operation."""

    try:
        api_key = str(settings.qdrant_api_key or "").strip()
        headers: dict[str, str] = {"api-key": api_key} if api_key else {}
        base_url = str(settings.qdrant_url).rstrip("/")
        async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
            health = await safe_trusted_service_request(
                client,
                "GET",
                base_url,
                "/healthz",
                headers={"Accept": "application/json, text/plain", **headers},
                timeout_seconds=1.5,
                max_response_bytes=1024 * 1024,
                allowed_response_content_types=(
                    "application/json",
                    "application/problem+json",
                    "text/plain",
                ),
            )
            if health.status_code != 200:
                return False
            collections = await safe_trusted_service_request(
                client,
                "GET",
                base_url,
                "/collections",
                headers={"Accept": "application/json", **headers},
                timeout_seconds=1.5,
                max_response_bytes=2 * 1024 * 1024,
                allowed_response_content_types=(
                    "application/json",
                    "application/problem+json",
                    "text/plain",
                ),
            )
            if collections.status_code != 200:
                return False
            payload = collections.json()
            rows = (
                payload.get("result", {}).get("collections", [])
                if isinstance(payload, dict)
                else []
            )
            if not isinstance(rows, list):
                return False
            names = [
                str(item.get("name") or "").strip()
                for item in rows
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ]
            required_collection = (
                str(settings.memory_vector_collection or "").strip()
                if settings.memory_vector_index_enabled
                and settings.memory_vector_index_strict_startup_check
                else ""
            )
            if required_collection and required_collection not in names:
                return False
            probe_collection = required_collection or (names[0] if names else "")
            if not probe_collection:
                # Listing collections is itself a semantic Qdrant API call.
                return True
            count = await safe_trusted_service_request(
                client,
                "POST",
                base_url,
                f"/collections/{quote(probe_collection, safe='')}/points/count",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    **headers,
                },
                json={"exact": False},
                timeout_seconds=1.5,
                max_response_bytes=2 * 1024 * 1024,
                allowed_response_content_types=(
                    "application/json",
                    "application/problem+json",
                    "text/plain",
                ),
            )
            if count.status_code != 200:
                return False
            result = count.json()
            return isinstance(result, dict) and isinstance(result.get("result"), dict)
    except Exception:
        return False


async def probe_wxbot_sdk_semantics(
    settings: Settings,
    *,
    sdk_url: str | None = None,
    sdk_headers: Mapping[str, str] | None = None,
    attempts: int = 1,
    timeout_seconds: float = 2.0,
    retry_delay_seconds: float = 0.25,
) -> bool:
    """Verify the optional WeChat SDK transport, auth and bot identity.

    A live bridge process or a successful TCP connection is not sufficient:
    the SDK status contract is healthy only when its remote authorization is
    active and it can resolve the account identity used to send messages.
    """

    base_url = str(sdk_url or settings.wxbot_sdk_url or "").strip().rstrip("/")
    if not base_url:
        return False
    attempt_count = max(1, int(attempts))
    request_timeout = max(0.1, float(timeout_seconds))
    retry_delay = max(0.0, float(retry_delay_seconds))
    headers = {
        "Accept": "application/json",
        **(
            dict(sdk_headers)
            if sdk_headers is not None
            else wxbot_sdk_headers(settings)
        ),
    }
    for attempt in range(attempt_count):
        try:
            async with httpx.AsyncClient(timeout=request_timeout, trust_env=False) as client:
                response = await safe_trusted_service_request(
                    client,
                    "GET",
                    base_url,
                    "/status",
                    headers=headers,
                    timeout_seconds=request_timeout,
                    max_response_bytes=2 * 1024 * 1024,
                    allowed_response_content_types=(
                        "application/json",
                        "application/problem+json",
                        "text/plain",
                    ),
                )
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict):
                    identity = payload.get("identity")
                    identity_ready = (
                        isinstance(identity, dict) and identity.get("ready") is True
                    )
                    if not identity_ready and identity is None:
                        # One-release compatibility for companions that predate the
                        # structured identity envelope. Their status contract exposes the
                        # resolved account only through the redacted configuration summary.
                        # Keep this fail-closed for absent, malformed, or empty identities.
                        legacy_config = payload.get("config")
                        identity_ready = isinstance(legacy_config, dict) and bool(
                            str(legacy_config.get("self_wxid") or "").strip()
                        )
                    if (
                        payload.get("status") == "running"
                        and payload.get("auth_active") is True
                        and identity_ready
                    ):
                        return True
        except Exception:
            pass
        if attempt + 1 < attempt_count and retry_delay:
            await asyncio.sleep(retry_delay)
    return False


async def probe_role_dependencies(
    role: str,
    settings: Settings,
    *,
    redis: Any | None = None,
    redis_probe: Callable[[], Awaitable[bool]] | None = None,
    db_probe: Callable[[], Awaitable[bool]] | None = None,
    qdrant_probe: Callable[[], Awaitable[bool]] | None = None,
    wxbot_sdk_probe: Callable[[], Awaitable[bool]] | None = None,
) -> RoleReadiness:
    normalized = str(role or "").strip().lower()
    required = required_dependencies_for_role(normalized, settings)
    if normalized not in PROCESS_ROLES:
        return RoleReadiness(
            role=normalized,
            checks={},
            required=(),
            errors=("unknown_role",),
        )

    probes: dict[str, Callable[[], Awaitable[bool]]] = {
        "redis": redis_probe or (lambda: probe_redis_semantics(redis)),
        "db": db_probe or probe_db_semantics,
        "qdrant": qdrant_probe or (lambda: probe_qdrant_semantics(settings)),
        "wxbot_sdk": wxbot_sdk_probe
        or (lambda: probe_wxbot_sdk_semantics(settings)),
    }
    values = await asyncio.gather(
        *(probes[name]() for name in required),
        return_exceptions=True,
    )
    checks = {
        name: bool(value) if not isinstance(value, BaseException) else False
        for name, value in zip(required, values, strict=True)
    }
    errors = tuple(f"{name}_unreachable" for name in required if not checks[name])
    return RoleReadiness(
        role=normalized,
        checks=checks,
        required=required,
        errors=errors,
    )


async def ensure_role_dependencies_ready(
    role: str,
    settings: Settings,
    *,
    redis: Any | None = None,
    wxbot_sdk_probe: Callable[[], Awaitable[bool]] | None = None,
) -> RoleReadiness:
    readiness = await probe_role_dependencies(
        role,
        settings,
        redis=redis,
        wxbot_sdk_probe=wxbot_sdk_probe,
    )
    if not readiness.ready:
        raise RoleDependenciesUnavailable(readiness.role, readiness.errors)
    return readiness


__all__ = [
    "PROCESS_ROLES",
    "WORKER_ROLES",
    "RoleDependenciesUnavailable",
    "RoleReadiness",
    "ensure_role_dependencies_ready",
    "probe_db_semantics",
    "probe_qdrant_semantics",
    "probe_redis_semantics",
    "probe_role_dependencies",
    "probe_wxbot_sdk_semantics",
    "required_dependencies_for_role",
]
