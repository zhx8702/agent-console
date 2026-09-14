"""Public per-group webhook for third-party text push."""

from __future__ import annotations

import time
from typing import Any

import orjson
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.channel.adapters import ChannelAdapterCatalog
from app.channel.connections import ChannelConnectionStore
from app.common.config import get_settings
from app.common.context import set_tenant_id, set_trace_id
from app.common.ids import new_trace_id
from app.common.logging import get_logger
from app.infra.db import get_session_factory
from app.infra.redis_client import get_redis
from app.ingress.ratelimit import TokenBucketRateLimiter
from plugins.wxbot.channel import WxbotChannelOutbound
from plugins.wxbot.group_webhook import (
    get_group_webhook_by_token,
    send_group_webhook_text,
    touch_group_webhook,
)
from plugins.wxbot.store import WxbotStore

logger = get_logger(__name__)


class GroupWebhookPushRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    message_id: str = Field(default="", max_length=128)


def _wxbot_store(container: Any) -> WxbotStore:
    store = getattr(container, "wxbot_store", None)
    if isinstance(store, WxbotStore):
        return store
    registry = getattr(container, "plugin_registry", None)
    plugins = getattr(registry, "_plugins", None) if registry is not None else None
    plugin = plugins.get("wxbot") if isinstance(plugins, dict) else None
    store = getattr(plugin, "_store", None)
    if not isinstance(store, WxbotStore):
        raise HTTPException(status_code=503, detail="wxbot_unavailable")
    return store


def _outbound(container: Any, store: WxbotStore) -> WxbotChannelOutbound:
    plugin_outbound = None
    registry = getattr(container, "plugin_registry", None)
    plugins = getattr(registry, "_plugins", None) if registry is not None else None
    plugin = plugins.get("wxbot") if isinstance(plugins, dict) else None
    plugin_outbound = getattr(plugin, "_channel_outbound", None)
    if isinstance(plugin_outbound, WxbotChannelOutbound):
        return plugin_outbound
    return WxbotChannelOutbound(
        store,
        social_policy_store=getattr(container, "social_policy_store", None),
        connection_store=ChannelConnectionStore(
            get_session_factory(),
            ChannelAdapterCatalog([]),
        ),
    )


async def _check_idempotency(
    redis: Redis,
    *,
    webhook_id: str,
    message_id: str,
    ttl_seconds: int,
) -> bool:
    key = f"group-webhook-idempotency:{webhook_id}:{message_id}"
    ok = await redis.set(key, "1", nx=True, ex=ttl_seconds)
    return bool(ok)


def build_group_webhook_router(container: Any) -> APIRouter:
    settings = get_settings()
    router = APIRouter()
    redis = get_redis()
    limiter = TokenBucketRateLimiter(
        redis,
        capacity=settings.inbound_default_rate_limit,
        refill_per_second=float(settings.inbound_default_rate_limit),
    )

    @router.post("/v1/webhook/groups/{webhook_token}")
    async def push_group_message(
        webhook_token: str,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        record = await get_group_webhook_by_token(webhook_token)
        if record is None or not record.active:
            raise HTTPException(status_code=404, detail="webhook_not_found")
        set_tenant_id(record.tenant_id)
        body = await request.body()
        if len(body) > settings.inbound_max_body_bytes:
            raise HTTPException(status_code=413, detail="body_too_large")
        try:
            payload = GroupWebhookPushRequest.model_validate(orjson.loads(body or b"{}"))
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid_json") from exc
        text = payload.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="text_required")
        allowed = await limiter.allow(
            f"group-webhook:{record.webhook_id}",
            now_ms=int(time.time() * 1000),
        )
        if not allowed:
            raise HTTPException(status_code=429, detail="rate_limited")
        message_id = str(payload.message_id or idempotency_key or "").strip()
        if message_id:
            is_new = await _check_idempotency(
                redis,
                webhook_id=record.webhook_id,
                message_id=message_id,
                ttl_seconds=settings.inbound_idempotency_ttl_seconds,
            )
            if not is_new:
                return JSONResponse(
                    {"status": "duplicate", "webhook_id": record.webhook_id},
                    status_code=200,
                )
        trace_id = new_trace_id()
        set_trace_id(trace_id)
        store = _wxbot_store(container)
        outbound = _outbound(container, store)
        try:
            result = await send_group_webhook_text(
                outbound,
                record,
                text=text,
                message_id=message_id,
                trace_id=trace_id,
            )
        except Exception as exc:
            logger.warning(
                "wxbot.group_webhook_send_failed",
                webhook_id=record.webhook_id,
                error_class=exc.__class__.__name__,
            )
            raise HTTPException(status_code=503, detail="send_failed") from exc
        metadata = getattr(result, "metadata", None)
        if isinstance(metadata, dict) and metadata.get("suppressed"):
            raise HTTPException(
                status_code=409,
                detail=str(metadata.get("reason") or "suppressed"),
            )
        await touch_group_webhook(record.webhook_id)
        return JSONResponse(
            {
                "status": "accepted",
                "webhook_id": record.webhook_id,
                "queue_id": str(getattr(result, "message_id", "") or ""),
                "trace_id": trace_id,
            },
            status_code=202,
        )

    return router
