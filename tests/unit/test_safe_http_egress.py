from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable

import httpx
import pytest

from app.common.safe_url import (
    OutboundURLPolicy,
    UnsafeOutboundResponseError,
    UnsafeOutboundURLError,
    normalize_origin,
)
from app.egress.safe_http import (
    safe_http_request,
    safe_trusted_service_request,
    safe_trusted_service_stream,
    trusted_service_url,
)


class _CaptureTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        responder: Callable[[httpx.Request], httpx.Response],
    ) -> None:
        self._responder = responder
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)


class _SlowStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(0.2)
        yield b'{}'

    async def aclose(self) -> None:
        return None


def _dns_record(address: str, port: int = 443) -> list[tuple[object, ...]]:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr: tuple[object, ...] = (
        (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    )
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


def _json_policy(**overrides: object) -> OutboundURLPolicy:
    values = {
        "require_https": True,
        "max_redirects": 0,
        "max_response_bytes": 1024,
        "timeout_seconds": 2.0,
        "allowed_response_content_types": ("application/json",),
    }
    values.update(overrides)
    return OutboundURLPolicy(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_safe_http_pins_validated_dns_address_and_preserves_host_and_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns_record("93.184.216.34"),
    )
    transport = _CaptureTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"ok":true}',
            request=request,
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        response = await safe_http_request(
            client,
            "GET",
            "https://feed.example.test/v1/items",
            headers={"Accept": "application/json"},
            policy=_json_policy(allowed_hosts=frozenset({"feed.example.test"})),
        )

    assert response.url == "https://feed.example.test/v1/items"
    assert len(transport.requests) == 1
    sent = transport.requests[0]
    assert sent.url.host == "93.184.216.34"
    assert sent.headers["host"] == "feed.example.test"
    assert sent.extensions["sni_hostname"] == "feed.example.test"
    assert sent.extensions["timeout"] == {
        "connect": 2.0,
        "read": 2.0,
        "write": 2.0,
        "pool": 2.0,
    }


@pytest.mark.asyncio
async def test_safe_http_rejects_mixed_public_private_dns_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            *_dns_record("93.184.216.34"),
            *_dns_record("10.20.30.40"),
        ],
    )
    transport = _CaptureTransport(lambda request: httpx.Response(204, request=request))

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(UnsafeOutboundURLError, match="non-public"):
            await safe_http_request(
                client,
                "GET",
                "https://mixed.example.test/data",
                policy=_json_policy(),
            )

    assert transport.requests == []


@pytest.mark.asyncio
async def test_safe_http_re_resolves_redirect_and_blocks_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = iter(("93.184.216.34", "127.0.0.1"))
    resolution_count = 0

    def resolve(*_args, **_kwargs):
        nonlocal resolution_count
        resolution_count += 1
        return _dns_record(next(resolutions))

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    transport = _CaptureTransport(
        lambda request: httpx.Response(
            302,
            headers={"location": "/second-hop"},
            request=request,
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(UnsafeOutboundURLError, match="non-public"):
            await safe_http_request(
                client,
                "GET",
                "https://redirect.example.test/start",
                policy=_json_policy(max_redirects=1),
            )

    assert resolution_count == 2
    assert len(transport.requests) == 1
    assert transport.requests[0].url.host == "93.184.216.34"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    ["169.254.169.254", "100.100.100.200", "[fd00:ec2::254]"],
)
async def test_safe_http_never_allows_metadata_even_for_private_service_origin(
    address: str,
) -> None:
    url = f"http://{address}/latest/meta-data"
    transport = _CaptureTransport(lambda request: httpx.Response(200, request=request))
    policy = _json_policy(
        require_https=False,
        allowed_private_origins=frozenset({normalize_origin(url)}),
    )

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(UnsafeOutboundURLError, match=r"forbidden|metadata"):
            await safe_http_request(client, "GET", url, policy=policy)

    assert transport.requests == []


@pytest.mark.asyncio
async def test_safe_http_rejects_post_redirect_and_does_not_forward_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns_record("93.184.216.34"),
    )
    transport = _CaptureTransport(
        lambda request: httpx.Response(
            307,
            headers={"location": "https://attacker.example.test/collect"},
            request=request,
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(UnsafeOutboundURLError, match="POST redirects"):
            await safe_http_request(
                client,
                "POST",
                "https://hooks.example.test/send",
                headers={"Authorization": "Bearer secret", "X-Signature": "secret"},
                json={"message": "safe"},
                policy=_json_policy(max_redirects=3),
            )

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_safe_http_strips_credentials_on_cross_origin_get_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns_record("93.184.216.34"),
    )
    call_count = 0

    def responder(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                302,
                headers={"location": "https://cdn.example.test/data"},
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"ok":true}',
            request=request,
        )

    transport = _CaptureTransport(responder)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await safe_http_request(
            client,
            "GET",
            "https://api.example.test/start",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer secret",
                "X-Signature": "secret",
            },
            policy=_json_policy(max_redirects=1),
        )

    assert response.json() == {"ok": True}
    assert len(transport.requests) == 2
    assert transport.requests[0].headers["authorization"] == "Bearer secret"
    assert transport.requests[0].headers["x-signature"] == "secret"
    assert "authorization" not in transport.requests[1].headers
    assert "x-signature" not in transport.requests[1].headers
    assert transport.requests[1].headers["accept"] == "application/json"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "content", "match"),
    [
        ({"content-type": "text/html"}, b"<html>bad</html>", "content-type"),
        (
            {"content-type": "application/json", "content-length": "2048"},
            b"{}",
            "exceeds 1024",
        ),
        ({"content-type": "application/json"}, b"x" * 1025, "exceeds 1024"),
    ],
)
async def test_safe_http_bounds_response_type_and_size(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    content: bytes,
    match: str,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns_record("93.184.216.34"),
    )
    transport = _CaptureTransport(
        lambda request: httpx.Response(
            200,
            headers=headers,
            content=content,
            request=request,
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(UnsafeOutboundResponseError, match=match):
            await safe_http_request(
                client,
                "GET",
                "https://feed.example.test/data",
                policy=_json_policy(),
            )


@pytest.mark.asyncio
async def test_safe_http_applies_a_total_response_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns_record("93.184.216.34"),
    )
    transport = _CaptureTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_SlowStream(),
            request=request,
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.TimeoutException, match="timed out"):
            await safe_http_request(
                client,
                "GET",
                "https://feed.example.test/data",
                policy=_json_policy(timeout_seconds=0.1),
            )


@pytest.mark.asyncio
async def test_trusted_service_mode_allows_only_the_exact_configured_private_origin() -> None:
    transport = _CaptureTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"ok":true}',
            request=request,
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        response = await safe_trusted_service_request(
            client,
            "GET",
            "http://127.0.0.1:5080",
            "/status",
            headers={"Authorization": "Bearer sdk-secret"},
            timeout_seconds=2.0,
            max_response_bytes=1024,
            allowed_response_content_types=("application/json",),
        )

    assert response.json() == {"ok": True}
    assert len(transport.requests) == 1
    assert transport.requests[0].headers["host"] == "127.0.0.1:5080"
    assert transport.requests[0].headers["authorization"] == "Bearer sdk-secret"
    with pytest.raises(UnsafeOutboundURLError, match="origin-relative"):
        trusted_service_url(
            "http://127.0.0.1:5080",
            "//attacker.example/collect",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "content", "match"),
    [
        ({"content-type": "application/json"}, b"event", "content-type"),
        ({"content-type": "text/event-stream"}, b"123456", "exceeds 5"),
    ],
)
async def test_trusted_service_stream_bounds_type_and_size(
    headers: dict[str, str],
    content: bytes,
    match: str,
) -> None:
    transport = _CaptureTransport(
        lambda request: httpx.Response(
            200,
            headers=headers,
            content=content,
            request=request,
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(UnsafeOutboundResponseError, match=match):
            async with safe_trusted_service_stream(
                client,
                "http://127.0.0.1:5080",
                "/stream",
                timeout_seconds=2.0,
                max_response_bytes=5,
            ) as response:
                async for _ in response.aiter_bytes():
                    pass


@pytest.mark.asyncio
async def test_trusted_service_stream_supports_bounded_post_json() -> None:
    transport = _CaptureTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"messages":[]}',
            request=request,
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        async with safe_trusted_service_stream(
            client,
            "http://127.0.0.1:5080",
            "/ext/persona/messages",
            method="POST",
            json={"session_id": "room", "max_messages": 0},
            headers={"Authorization": "Bearer sdk-secret"},
            timeout_seconds=2.0,
            max_response_bytes=1024,
            allowed_response_content_types=("application/json",),
        ) as response:
            body = b"".join([chunk async for chunk in response.aiter_bytes()])

    assert body == b'{"messages":[]}'
    assert len(transport.requests) == 1
    assert transport.requests[0].method == "POST"
    assert json.loads(transport.requests[0].content) == {
        "session_id": "room",
        "max_messages": 0,
    }
    assert transport.requests[0].headers["authorization"] == "Bearer sdk-secret"


@pytest.mark.asyncio
async def test_trusted_service_stream_has_a_total_connection_deadline() -> None:
    transport = _CaptureTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_SlowStream(),
            request=request,
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.TimeoutException, match="stream timed out"):
            async with safe_trusted_service_stream(
                client,
                "http://127.0.0.1:5080",
                "/stream",
                timeout_seconds=0.1,
                max_response_bytes=1024,
            ) as response:
                async for _ in response.aiter_bytes():
                    pass
