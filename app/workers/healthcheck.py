from __future__ import annotations

import argparse
import asyncio
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

from app.common.config import Settings, get_settings
from app.infra.redis_client import get_redis
from app.workers.readiness import (
    WORKER_ROLES,
    RoleReadiness,
    probe_role_dependencies,
)

_WORKER_ROLE_CHOICES = tuple(sorted(WORKER_ROLES))


async def worker_is_ready(
    role: str,
    *,
    settings: Settings,
    redis: Any,
    hostname: str | None = None,
    dependency_probe: (
        Callable[[str, Settings, Any], Awaitable[RoleReadiness]] | None
    ) = None,
) -> bool:
    normalized_role = role.strip().lower()
    if normalized_role not in WORKER_ROLES:
        return False
    explicit_instance_id = str(settings.worker_instance_id or "").strip()
    if explicit_instance_id:
        instance_pattern = quote(explicit_instance_id, safe="")
    else:
        instance_pattern = f"{quote(hostname or socket.gethostname(), safe='')}-*"
    pattern = ":".join(
        (
            settings.worker_heartbeat_key_prefix.rstrip(":"),
            quote(normalized_role, safe=""),
            instance_pattern,
            "*",
        )
    )
    heartbeat_alive = False
    try:
        async for key in redis.scan_iter(match=pattern, count=20):
            if int(await redis.ttl(key)) > 0:
                heartbeat_alive = True
                break
    except Exception:
        return False
    if not heartbeat_alive:
        return False

    try:
        if dependency_probe is None:
            readiness = await probe_role_dependencies(
                normalized_role,
                settings,
                redis=redis,
            )
        else:
            readiness = await dependency_probe(normalized_role, settings, redis)
    except Exception:
        return False
    return readiness.ready


async def _run(role: str) -> bool:
    return await worker_is_ready(
        role,
        settings=get_settings(),
        redis=get_redis(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check whether a worker role has a live ready heartbeat."
    )
    parser.add_argument("--role", required=True, choices=_WORKER_ROLE_CHOICES)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(_run(args.role)) else 1)


if __name__ == "__main__":
    main()
