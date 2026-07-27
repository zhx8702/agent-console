"""
Outbound Dispatcher (M14).

Consumes outbound reply events from the bus and POSTs them to the tenant's
configured outbound webhook. HMAC-signs with that tenant's key and includes
``X-Trace-Id``.

Retry policy:
- 2xx: success -> ack.
- 4xx (except 429): permanent client error -> move to DLQ, ack.
- 429 or 5xx: transient -> raise ``UpstreamUnavailable``; the bus-level
  retry logic (in RedisStreamBus) will re-enqueue with backoff up to
  ``settings.outbound_max_retries`` attempts, then DLQ.
- In-process retries with exponential backoff are handled by tenacity around
  the HTTP call itself, catching transient network errors.
"""
from __future__ import annotations

import asyncio
from urllib.parse import quote

import httpx
import orjson
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.bus.base import BusMessage, MessageBus
from app.common.config import Settings
from app.common.exceptions import UpstreamUnavailable
from app.common.hashing import hmac_sha256
from app.common.logging import get_logger
from app.common.safe_url import (
    OutboundURLPolicy,
    UnsafeOutboundURLError,
    normalize_origin,
)
from app.common.types import OutboundReply
from app.egress.safe_http import safe_http_request
from app.infra.metrics import DLQ_SIZE, OUTBOUND_RETRIES, OUTBOUND_SENT

logger = get_logger(__name__)


class OutboundDispatcher:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        bus: MessageBus,
        settings: Settings,
    ) -> None:
        self._http = http_client
        self._bus = bus
        self._settings = settings
        self._worker_prepared = False
        self._worker_prepare_lock = asyncio.Lock()

    # -- public: direct send --------------------------------------------------

    async def send(self, reply: OutboundReply) -> None:
        """POST the reply to the outbound webhook.

        Raises UpstreamUnavailable on 429/5xx/network errors so the caller can
        decide whether to retry at the bus level. Moves 4xx (non-429) to DLQ
        immediately with reason='client_error'.
        """
        tenant = reply.tenant_id
        url = self._settings.get_tenant_outbound_webhook_url(tenant)
        signing_secret = self._settings.get_tenant_outbound_hmac_secret(tenant)
        if not url or not signing_secret:
            await self._move_to_dlq(reply, reason="tenant_delivery_not_configured")
            logger.error(
                "outbound.tenant_delivery_not_configured",
                tenant_id=tenant,
                reply_id=reply.reply_id,
                trace_id=reply.trace_id,
            )
            return

        body = orjson.dumps(reply.model_dump(mode="json"))
        signature = hmac_sha256(signing_secret, body)
        headers = {
            "Content-Type": "application/json",
            "X-Signature": signature,
            "X-Trace-Id": reply.trace_id or "",
            "Idempotency-Key": reply.reply_id,
        }

        try:
            resp = await self._post_with_retry(url, body, headers, tenant)
        except RetryError as e:
            # tenacity exhausted retries; always caused by UpstreamUnavailable.
            logger.warning(
                "outbound.retry_exhausted",
                reply_id=reply.reply_id,
                trace_id=reply.trace_id,
            )
            last = e.last_attempt.exception() if e.last_attempt else None
            if isinstance(last, UpstreamUnavailable):
                raise last from e
            raise UpstreamUnavailable("outbound_retry_exhausted") from e
        except UnsafeOutboundURLError as e:
            await self._move_to_dlq(reply, reason="unsafe_destination")
            logger.error(
                "outbound.unsafe_destination",
                tenant_id=tenant,
                reply_id=reply.reply_id,
                trace_id=reply.trace_id,
                error_class=e.__class__.__name__,
            )
            return
        except httpx.HTTPError as e:
            OUTBOUND_SENT.labels(tenant=tenant, result="network_error").inc()
            raise UpstreamUnavailable(f"network_error:{e.__class__.__name__}") from e

        status_code = resp.status_code
        if 200 <= status_code < 300:
            OUTBOUND_SENT.labels(tenant=tenant, result="ok").inc()
            logger.info(
                "outbound.ok",
                reply_id=reply.reply_id,
                trace_id=reply.trace_id,
                status=status_code,
            )
            return

        if status_code == 429 or 500 <= status_code < 600:
            OUTBOUND_SENT.labels(tenant=tenant, result=f"status_{status_code}").inc()
            raise UpstreamUnavailable(f"status_{status_code}")

        # 4xx (non-429): permanent failure. DLQ directly, do not retry.
        OUTBOUND_SENT.labels(tenant=tenant, result=f"status_{status_code}").inc()
        logger.error(
            "outbound.client_error",
            reply_id=reply.reply_id,
            trace_id=reply.trace_id,
            status=status_code,
        )
        # Translate to a DLQ write via the bus. For direct `send` call (no
        # bus message context) we synthesize a BusMessage.
        await self._move_to_dlq(reply, reason="client_error")

    async def _post_with_retry(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        tenant: str,
    ) -> httpx.Response:
        """In-process retry for transient network errors only.

        5xx/429 status codes do NOT trigger retry here — they propagate up
        as UpstreamUnavailable so bus-level retry (with persisted attempts
        counter) can take over.
        """
        origin = normalize_origin(url)
        policy = OutboundURLPolicy(
            require_https=self._settings.is_prod,
            allowed_private_origins=(
                frozenset({origin})
                if origin and not self._settings.is_prod
                else frozenset()
            ),
            max_redirects=0,
            max_response_bytes=64 * 1024,
            timeout_seconds=self._settings.outbound_timeout_seconds,
            allowed_response_content_types=(
                "application/json",
                "application/problem+json",
                "text/plain",
            ),
        )

        attempt_counter = {"n": 0}
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._settings.outbound_transport_max_attempts),
            wait=wait_exponential(
                multiplier=self._settings.outbound_transport_retry_base_seconds,
                min=self._settings.outbound_transport_retry_base_seconds,
                max=self._settings.outbound_transport_retry_max_seconds,
            ),
            retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
            reraise=True,
        ):
            with attempt:
                attempt_counter["n"] += 1
                if attempt_counter["n"] > 1:
                    OUTBOUND_RETRIES.labels(tenant=tenant).inc()
                resp = await safe_http_request(
                    self._http,
                    "POST",
                    url,
                    content=body,
                    headers=headers,
                    policy=policy,
                )
                return resp
        # Unreachable: AsyncRetrying either returns via the `return resp`
        # above or raises.
        raise RuntimeError("unreachable")

    async def _move_to_dlq(self, reply: OutboundReply, *, reason: str) -> None:
        synthetic = BusMessage(
            id=reply.reply_id,
            stream=self._settings.bus_outbound_stream,
            payload=reply.model_dump(mode="json"),
            headers={
                "trace_id": reply.trace_id or "",
                "tenant_id": reply.tenant_id,
                "adapter_id": reply.adapter_id,
                "connection_id": reply.connection_id,
            },
            attempts=0,
        )
        DLQ_SIZE.labels(reason=reason).inc()
        await self._bus.move_to_dlq(synthetic, reason=reason)

    # -- bus worker -----------------------------------------------------------

    async def prepare_worker(self) -> None:
        """Initialize the outbound stream dependency before advertising readiness."""
        if self._worker_prepared:
            return
        async with self._worker_prepare_lock:
            if self._worker_prepared:
                return
            await self._bus.ensure_group(
                self._settings.bus_outbound_stream,
                self._settings.bus_consumer_group,
            )
            self._worker_prepared = True

    async def run_worker(self) -> None:
        """Consume outbound stream and dispatch. Meant to be launched as a task."""
        stream = self._settings.bus_outbound_stream
        group = self._settings.bus_consumer_group
        consumer = self._settings.resolved_outbound_worker_consumer_name

        async def handler(msg: BusMessage) -> None:
            reply = OutboundReply.model_validate(msg.payload)
            await self.send(reply)

        await self.prepare_worker()
        async for _ in self._bus.consume(
            stream,
            group,
            consumer,
            handler,
            batch_size=self._settings.bus_consume_batch_size,
            block_ms=self._settings.bus_consume_block_ms,
        ):
            pass

    # -- enqueue helper (used by postprocessor/orchestrator) -----------------

    async def enqueue(self, reply: OutboundReply) -> str:
        """Publish reply onto the outbound bus with per-session ordering."""
        return await self._bus.publish(
            self._settings.bus_outbound_stream,
            reply.model_dump(mode="json"),
            headers={
                "tenant_id": reply.tenant_id,
                "trace_id": reply.trace_id or "",
                "session_id": reply.session_id,
                "adapter_id": reply.adapter_id,
                "connection_id": reply.connection_id,
            },
            partition_key=(
                f"{quote(reply.tenant_id, safe='')}:"
                f"{quote(reply.connection_id, safe='')}:"
                f"{quote(reply.session_id, safe='')}"
                if reply.connection_id
                else (
                    f"{quote(reply.tenant_id, safe='')}:"
                    f"{quote(reply.session_id, safe='')}"
                )
            ),
        )
