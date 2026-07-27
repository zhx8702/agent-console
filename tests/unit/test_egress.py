"""
Tests for the Outbound Dispatcher (M14).

Uses httpx.MockTransport to simulate the outbound webhook.
"""
from __future__ import annotations

from collections.abc import Callable

import httpx
import orjson
import pytest

from app.common.config import get_settings
from app.common.exceptions import UpstreamUnavailable
from app.common.hashing import hmac_sha256
from app.common.types import Channel, OutboundReply, ReplySegment, ReplyType
from app.egress.dispatcher import OutboundDispatcher
from tests.unit._fakes import InMemoryBus


def _make_reply(**overrides: object) -> OutboundReply:
    data = {
        "tenant_id": "demo",
        "channel": Channel.WEB,
        "user_id": "u1",
        "session_id": "se_test00000000000001",
        "type": ReplyType.TEXT,
        "segments": [ReplySegment(type=ReplyType.TEXT, content="hi")],
        "trace_id": "tr_abc",
    }
    data.update(overrides)  # type: ignore[arg-type]
    return OutboundReply(**data)  # type: ignore[arg-type]


def _transport(responder: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(responder)


class _CapturingBus(InMemoryBus):
    def __init__(self) -> None:
        super().__init__()
        self.consume_args: tuple[str, str, str, int, int] | None = None
        self.ensure_group_calls = 0

    async def ensure_group(self, stream: str, group: str) -> None:
        self.ensure_group_calls += 1
        await super().ensure_group(stream, group)

    async def consume(  # type: ignore[override]
        self,
        stream: str,
        group: str,
        consumer: str,
        handler: Callable[[object], object],
        *,
        batch_size: int = 16,
        block_ms: int = 5_000,
    ):
        _ = handler
        self.consume_args = (stream, group, consumer, batch_size, block_ms)
        if False:
            yield None
        return


@pytest.mark.asyncio
async def test_happy_path_2xx() -> None:
    s = get_settings()
    bus = InMemoryBus()
    seen: dict[str, object] = {}

    def responder(req: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(req.headers)
        seen["content"] = bytes(req.content)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=_transport(responder)) as http:
        disp = OutboundDispatcher(http_client=http, bus=bus, settings=s)  # type: ignore[arg-type]
        reply = _make_reply()
        await disp.send(reply)

    content = seen["content"]
    assert isinstance(content, (bytes, bytearray))
    body_bytes = bytes(content)
    parsed = orjson.loads(body_bytes)
    assert parsed["tenant_id"] == "demo"
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["x-trace-id"] == "tr_abc"
    expected_sig = hmac_sha256(s.outbound_hmac_secret, body_bytes)
    assert headers["x-signature"] == expected_sig
    assert not bus.dlq


@pytest.mark.asyncio
async def test_tenant_specific_destination_and_signing_key_are_isolated() -> None:
    settings = get_settings().model_copy(
        update={
            "tenant_outbound_webhook_urls": {
                "tenant-a": "https://a.example.test/deliver",
                "tenant-b": "https://b.example.test/deliver",
            },
            "tenant_outbound_hmac_secrets": {
                "tenant-a": "secret-a",
                "tenant-b": "secret-b",
            },
        }
    )
    bus = InMemoryBus()
    seen: dict[str, object] = {}

    def responder(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["signature"] = req.headers["x-signature"]
        seen["body"] = bytes(req.content)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=_transport(responder)) as http:
        dispatcher = OutboundDispatcher(http_client=http, bus=bus, settings=settings)
        await dispatcher.send(_make_reply(tenant_id="tenant-b"))

    assert seen["url"] == "https://b.example.test/deliver"
    assert seen["signature"] == hmac_sha256("secret-b", seen["body"])  # type: ignore[arg-type]
    assert not bus.dlq


@pytest.mark.asyncio
async def test_unknown_tenant_delivery_fails_closed_to_dlq() -> None:
    settings = get_settings().model_copy(
        update={
            "tenant_outbound_webhook_urls": {"tenant-a": "https://a.example.test/deliver"},
            "tenant_outbound_hmac_secrets": {"tenant-a": "secret-a"},
        }
    )
    bus = InMemoryBus()
    calls = 0

    def responder(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=_transport(responder)) as http:
        dispatcher = OutboundDispatcher(http_client=http, bus=bus, settings=settings)
        await dispatcher.send(_make_reply(tenant_id="unknown"))

    assert calls == 0
    assert len(bus.dlq) == 1
    assert bus.dlq[0][1] == "tenant_delivery_not_configured"


@pytest.mark.asyncio
async def test_production_rejects_insecure_tenant_destination() -> None:
    settings = get_settings().model_copy(
        update={
            "app_env": "prod",
            "tenant_outbound_webhook_urls": {
                "tenant-a": "http://public.example.test/deliver",
            },
            "tenant_outbound_hmac_secrets": {"tenant-a": "secret-a"},
        }
    )
    bus = InMemoryBus()

    async with httpx.AsyncClient(
        transport=_transport(lambda req: httpx.Response(204))
    ) as http:
        dispatcher = OutboundDispatcher(http_client=http, bus=bus, settings=settings)
        await dispatcher.send(_make_reply(tenant_id="tenant-a"))

    assert len(bus.dlq) == 1
    assert bus.dlq[0][1] == "unsafe_destination"


@pytest.mark.asyncio
async def test_client_error_goes_to_dlq_no_retry() -> None:
    s = get_settings()
    bus = InMemoryBus()
    calls = {"n": 0}

    def responder(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad"})

    async with httpx.AsyncClient(transport=_transport(responder)) as http:
        disp = OutboundDispatcher(http_client=http, bus=bus, settings=s)  # type: ignore[arg-type]
        await disp.send(_make_reply())

    # Only one call: 4xx is permanent.
    assert calls["n"] == 1
    assert len(bus.dlq) == 1
    _, reason = bus.dlq[0]
    assert reason == "client_error"


@pytest.mark.asyncio
async def test_server_error_raises_upstream_unavailable() -> None:
    s = get_settings()
    bus = InMemoryBus()

    def responder(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    async with httpx.AsyncClient(transport=_transport(responder)) as http:
        disp = OutboundDispatcher(http_client=http, bus=bus, settings=s)  # type: ignore[arg-type]
        with pytest.raises(UpstreamUnavailable):
            await disp.send(_make_reply())

    # No DLQ: bus-level retry will handle it.
    assert not bus.dlq


@pytest.mark.asyncio
async def test_429_raises_upstream_unavailable() -> None:
    s = get_settings()
    bus = InMemoryBus()

    def responder(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    async with httpx.AsyncClient(transport=_transport(responder)) as http:
        disp = OutboundDispatcher(http_client=http, bus=bus, settings=s)  # type: ignore[arg-type]
        with pytest.raises(UpstreamUnavailable):
            await disp.send(_make_reply())
    assert not bus.dlq


@pytest.mark.asyncio
async def test_network_error_retries_then_upstream_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = get_settings()
    bus = InMemoryBus()
    calls = {"n": 0}

    def responder(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    # Shrink tenacity waits to keep the test fast.
    import tenacity

    real_wait = tenacity.wait_exponential

    def fast_wait(*args: object, **kwargs: object) -> object:
        _ = args, kwargs
        return tenacity.wait_fixed(0)

    monkeypatch.setattr("app.egress.dispatcher.wait_exponential", fast_wait)

    async with httpx.AsyncClient(transport=_transport(responder)) as http:
        disp = OutboundDispatcher(http_client=http, bus=bus, settings=s)  # type: ignore[arg-type]
        with pytest.raises(UpstreamUnavailable):
            await disp.send(_make_reply())

    assert calls["n"] >= 2  # retried at least once
    # Restore just in case.
    _ = real_wait


@pytest.mark.asyncio
async def test_enqueue_uses_session_partition_key() -> None:
    s = get_settings()
    bus = InMemoryBus()
    async with httpx.AsyncClient() as http:
        disp = OutboundDispatcher(http_client=http, bus=bus, settings=s)  # type: ignore[arg-type]
        reply = _make_reply()
        await disp.enqueue(reply)

    published = bus.streams[s.bus_outbound_stream]
    assert len(published) == 1
    assert published[0].headers.get("partition_key") == f"{reply.tenant_id}:{reply.session_id}"


@pytest.mark.asyncio
async def test_run_worker_uses_configured_consumer_name() -> None:
    settings = get_settings().model_copy(
        update={
            "worker_instance_id": "node-b-02",
            "outbound_worker_consumer_name": None,
            "bus_consume_batch_size": 8,
            "bus_consume_block_ms": 1200,
        }
    )
    bus = _CapturingBus()

    async with httpx.AsyncClient() as http:
        disp = OutboundDispatcher(http_client=http, bus=bus, settings=settings)  # type: ignore[arg-type]
        await disp.run_worker()

    assert bus.consume_args == (
        settings.bus_outbound_stream,
        settings.bus_consumer_group,
        "egress-node-b-02",
        8,
        1200,
    )
    assert bus.ensure_group_calls == 1


@pytest.mark.asyncio
async def test_prepare_worker_is_idempotent_before_run() -> None:
    settings = get_settings()
    bus = _CapturingBus()
    async with httpx.AsyncClient(transport=_transport(lambda _req: httpx.Response(200))) as http:
        dispatcher = OutboundDispatcher(http_client=http, bus=bus, settings=settings)
        await dispatcher.prepare_worker()
        await dispatcher.run_worker()

    assert bus.ensure_group_calls == 1
