from __future__ import annotations

import socket

import httpx
import pytest

from app.common.safe_url import (
    OutboundURLPolicy,
    UnsafeOutboundResponseError,
    UnsafeOutboundURLError,
    normalize_origin,
    safe_get,
    validate_outbound_url,
)


@pytest.mark.asyncio
async def test_safe_url_rejects_loopback_and_cloud_metadata_addresses() -> None:
    with pytest.raises(UnsafeOutboundURLError, match="non-public"):
        await validate_outbound_url("http://127.0.0.1/internal")
    with pytest.raises(UnsafeOutboundURLError, match="non-public"):
        await validate_outbound_url("http://169.254.169.254/latest/meta-data")


@pytest.mark.asyncio
async def test_safe_url_rejects_hostname_resolving_to_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(*args, **kwargs):
        _ = args, kwargs
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.20.30.40", 443),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(UnsafeOutboundURLError, match="non-public"):
        await validate_outbound_url("https://example.invalid/image.png")


@pytest.mark.asyncio
async def test_safe_url_allows_explicit_private_service_origin() -> None:
    policy = OutboundURLPolicy(
        allowed_private_origins=frozenset(
            {normalize_origin("http://127.0.0.1:5080")}
        )
    )

    value = await validate_outbound_url(
        "http://127.0.0.1:5080/images/preview.png",
        policy=policy,
    )

    assert value.endswith("/images/preview.png")


@pytest.mark.asyncio
async def test_safe_get_revalidates_redirect_target() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host == "8.8.8.8":
            return httpx.Response(
                302,
                headers={"location": "http://127.0.0.1/admin"},
                request=request,
            )
        return httpx.Response(200, content=b"secret", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        with pytest.raises(UnsafeOutboundURLError, match="non-public"):
            await safe_get(client, "https://8.8.8.8/image.png")


@pytest.mark.asyncio
async def test_safe_url_requires_allowlisted_https_webhook() -> None:
    policy = OutboundURLPolicy(
        require_https=True,
        allowed_hosts=frozenset({"hooks.example.com"}),
    )

    with pytest.raises(UnsafeOutboundURLError, match="https"):
        await validate_outbound_url(
            "http://hooks.example.com/notify",
            policy=policy,
            resolve_dns=False,
        )
    with pytest.raises(UnsafeOutboundURLError, match="allowlist"):
        await validate_outbound_url(
            "https://attacker.example/notify",
            policy=policy,
            resolve_dns=False,
        )


@pytest.mark.asyncio
async def test_safe_get_rejects_non_image_response() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html>not an image</html>",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        with pytest.raises(UnsafeOutboundResponseError, match="content-type"):
            await safe_get(client, "https://8.8.8.8/image.png")


@pytest.mark.asyncio
async def test_safe_get_rejects_declared_and_streamed_oversize_responses() -> None:
    def declared(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png", "content-length": "6"},
            content=b"123456",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(declared)) as client:
        with pytest.raises(UnsafeOutboundResponseError, match="exceeds 5"):
            await safe_get(
                client,
                "https://8.8.8.8/image.png",
                max_response_bytes=5,
            )

    def chunked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=b"123456",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(chunked)) as client:
        with pytest.raises(UnsafeOutboundResponseError, match="exceeds 5"):
            await safe_get(
                client,
                "https://8.8.8.8/image.png",
                max_response_bytes=5,
            )


@pytest.mark.asyncio
async def test_safe_get_applies_explicit_timeout() -> None:
    captured: dict[str, object] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        captured.update(request.extensions.get("timeout", {}))
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=b"png",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        await safe_get(
            client,
            "https://8.8.8.8/image.png",
            timeout_seconds=2.5,
        )

    assert captured == {
        "connect": 2.5,
        "read": 2.5,
        "write": 2.5,
        "pool": 2.5,
    }
