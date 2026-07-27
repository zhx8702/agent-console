"""
Inbound webhook FastAPI router.

Responsibilities:
- Read raw body once, enforce body size limit.
- HMAC + timestamp signature check.
- Per-tenant token-bucket rate limit.
- Idempotency key via Redis SETNX.
- Parse into InboundEvent and publish to the bus (partitioned by session_id).

Wired via ``build_router(container)`` so main.py can inject the service
container.
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import orjson
from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from redis.asyncio import Redis

from app.common.config import Settings, get_settings
from app.common.context import set_tenant_id, set_trace_id
from app.common.exceptions import SignatureError
from app.common.ids import new_trace_id
from app.common.logging import get_logger
from app.common.types import InboundEvent, channel_id_value
from app.container import Container
from app.infra.metrics import INBOUND_LATENCY, INBOUND_RECEIVED
from app.infra.redis_client import get_redis
from app.ingress.ratelimit import TokenBucketRateLimiter
from app.ingress.signature import verify_signature

logger = get_logger(__name__)


def _idempotency_key(
    tenant_id: str,
    message_id: str,
    connection_id: str = "",
) -> str:
    """Build a replay fence scoped to the concrete message connection.

    Empty connection ids retain the one-release webhook key so existing
    integrations do not lose their replay fence during the expand phase.
    """

    connection = str(connection_id or "").strip()
    if connection:
        return (
            f"idempotency:{quote(tenant_id, safe='')}:"
            f"{quote(connection, safe='')}:{quote(message_id, safe='')}"
        )
    return (
        f"idempotency:{quote(tenant_id, safe='')}:"
        f"{quote(message_id, safe='')}"
    )


async def _check_idempotency(
    redis: Redis,
    *,
    tenant_id: str,
    message_id: str,
    ttl_seconds: int,
    connection_id: str = "",
) -> bool:
    """Return True if this is a new request (we set the key), False if replay."""
    key = _idempotency_key(tenant_id, message_id, connection_id)
    # set NX EX returns True on success (key was set), None/False if exists.
    ok = await redis.set(key, "1", nx=True, ex=ttl_seconds)
    return bool(ok)


def build_router(container: Container) -> APIRouter:
    """Build the inbound router, injected with the service container.

    main.py should call ``app.include_router(build_router(container))``.
    """
    settings = get_settings()
    router = APIRouter()

    # Redis is a shared singleton; the rate limiter is stateless across
    # requests apart from the loaded Lua script SHA.
    redis = get_redis()
    limiter = TokenBucketRateLimiter(
        redis,
        capacity=settings.inbound_default_rate_limit,
        refill_per_second=float(settings.inbound_default_rate_limit),
    )

    @router.post("/v1/webhook/inbound")
    async def inbound(
        request: Request,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_signature: str | None = Header(default=None, alias="X-Signature"),
        x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    ) -> JSONResponse:
        return await _handle_inbound(
            request=request,
            container=container,
            settings=settings,
            redis=redis,
            limiter=limiter,
            tenant_id=x_tenant_id,
            signature=x_signature,
            timestamp=x_timestamp,
        )

    return router


async def _handle_inbound(
    *,
    request: Request,
    container: Container,
    settings: Settings,
    redis: Redis,
    limiter: TokenBucketRateLimiter,
    tenant_id: str | None,
    signature: str | None,
    timestamp: str | None,
) -> JSONResponse:
    start = time.perf_counter()
    tenant_label = tenant_id or "unknown"
    channel_label = "unknown"

    try:
        if not tenant_id:
            raise HTTPException(status_code=400, detail="missing_tenant_id")

        set_tenant_id(tenant_id)

        # Read body once.
        body = await request.body()
        if len(body) > settings.inbound_max_body_bytes:
            INBOUND_RECEIVED.labels(
                tenant=tenant_label, channel=channel_label, result="too_large"
            ).inc()
            raise HTTPException(status_code=413, detail="body_too_large")

        # Signature + timestamp window.
        try:
            verify_signature(
                settings=settings,
                tenant_id=tenant_id,
                body=body,
                signature=signature,
                timestamp=timestamp,
            )
        except SignatureError as e:
            INBOUND_RECEIVED.labels(
                tenant=tenant_label, channel=channel_label, result="unauthorized"
            ).inc()
            raise HTTPException(status_code=401, detail=e.code) from e

        # Rate limit.
        if not await limiter.allow(tenant_id, now_ms=int(time.time() * 1000)):
            INBOUND_RECEIVED.labels(
                tenant=tenant_label, channel=channel_label, result="rate_limited"
            ).inc()
            raise HTTPException(status_code=429, detail="rate_limited")

        # Parse body.
        try:
            decoded: Any = orjson.loads(body)
        except orjson.JSONDecodeError as e:
            INBOUND_RECEIVED.labels(
                tenant=tenant_label, channel=channel_label, result="bad_json"
            ).inc()
            raise HTTPException(status_code=400, detail="invalid_json") from e

        if not isinstance(decoded, dict):
            INBOUND_RECEIVED.labels(
                tenant=tenant_label, channel=channel_label, result="bad_schema"
            ).inc()
            raise HTTPException(status_code=400, detail="invalid_schema:object_required")

        raw: dict[str, Any] = decoded
        payload_tenant_id = raw.get("tenant_id")
        if "tenant_id" in raw and payload_tenant_id != tenant_id:
            INBOUND_RECEIVED.labels(
                tenant=tenant_label, channel=channel_label, result="tenant_mismatch"
            ).inc()
            logger.warning(
                "inbound.tenant_mismatch",
                signed_tenant_id=tenant_id,
                payload_tenant_id=payload_tenant_id,
            )
            raise HTTPException(status_code=400, detail="tenant_id_mismatch")

        # The signed header is the authenticated tenant identity.  Never let
        # an unsigned payload field override it, even when the field is absent.
        raw["tenant_id"] = tenant_id
        try:
            event = InboundEvent.model_validate(raw)
        except PydanticValidationError as e:
            INBOUND_RECEIVED.labels(
                tenant=tenant_label, channel=channel_label, result="bad_schema"
            ).inc()
            raise HTTPException(status_code=400, detail=f"invalid_schema:{e.error_count()}") from e

        channel_label = channel_id_value(event.channel)
        # Override trace id context with event's own trace id so downstream
        # logs correlate cleanly.
        if not event.trace_id:
            event.trace_id = new_trace_id()
        set_trace_id(event.trace_id)

        # Idempotency.
        is_new = await _check_idempotency(
            redis,
            tenant_id=tenant_id,
            message_id=event.message_id,
            ttl_seconds=settings.inbound_idempotency_ttl_seconds,
            connection_id=str(getattr(event, "connection_id", "") or ""),
        )
        if not is_new:
            INBOUND_RECEIVED.labels(
                tenant=tenant_label, channel=channel_label, result="duplicate"
            ).inc()
            logger.info(
                "inbound.duplicate",
                tenant_id=tenant_id,
                message_id=event.message_id,
                trace_id=event.trace_id,
            )
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"status": "duplicate", "trace_id": event.trace_id},
            )

        # Publish to bus.
        if container.bus is None:
            logger.error("inbound.bus_missing")
            INBOUND_RECEIVED.labels(
                tenant=tenant_label, channel=channel_label, result="bus_unavailable"
            ).inc()
            raise HTTPException(status_code=503, detail="bus_unavailable")

        payload = event.model_dump(mode="json")
        headers = {
            "tenant_id": tenant_id,
            "trace_id": event.trace_id,
            "channel": channel_id_value(event.channel),
            "adapter_id": str(getattr(event, "adapter_id", "") or ""),
            "connection_id": str(getattr(event, "connection_id", "") or ""),
        }
        connection_id = str(getattr(event, "connection_id", "") or "").strip()
        partition_key = (
            f"{event.tenant_id}:{connection_id}:{event.session_id}"
            if connection_id
            else f"{event.tenant_id}:{event.session_id}"
        )
        try:
            await container.bus.publish(
                settings.bus_inbound_stream,
                payload,
                headers=headers,
                partition_key=partition_key,
            )
        except Exception as e:
            logger.exception("inbound.publish_failed", message_id=event.message_id)
            INBOUND_RECEIVED.labels(
                tenant=tenant_label, channel=channel_label, result="publish_error"
            ).inc()
            # Roll back idempotency key so upstream retry can succeed.
            try:
                await redis.delete(
                    _idempotency_key(tenant_id, event.message_id, connection_id)
                )
            except Exception:
                logger.exception("inbound.idempotency_rollback_failed")
            raise HTTPException(status_code=503, detail="publish_failed") from e

        INBOUND_RECEIVED.labels(
            tenant=tenant_label, channel=channel_label, result="accepted"
        ).inc()
        logger.info(
            "inbound.accepted",
            tenant_id=tenant_id,
            message_id=event.message_id,
            trace_id=event.trace_id,
            session_id=event.session_id,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"status": "accepted", "trace_id": event.trace_id},
        )
    finally:
        INBOUND_LATENCY.labels(tenant=tenant_label).observe(time.perf_counter() - start)
