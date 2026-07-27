"""Fail-closed HTTP egress with DNS pinning and bounded responses.

The shared URL policy validates the logical destination.  This module adds the
network invariant that closes the DNS-rebinding gap: a hostname is resolved
once, every returned address is checked, and the request connects to one of
those checked IP addresses while preserving the original Host header and TLS
SNI name.  Redirects repeat the complete validation and resolution process.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.common.safe_url import (
    OutboundURLPolicy,
    UnsafeOutboundResponseError,
    UnsafeOutboundURLError,
    normalize_origin,
    validate_outbound_url,
)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CLOUD_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


def trusted_service_policy(
    base_url: str,
    *,
    timeout_seconds: float,
    max_response_bytes: int,
    allowed_response_content_types: tuple[str, ...],
) -> OutboundURLPolicy:
    """Create a fail-closed policy for one configured service origin.

    This mode is for fixed infrastructure dependencies (for example the local
    wxbot SDK or Qdrant).  It permits private addresses only for the exact
    configured origin and never enables redirects.
    """

    parsed = urlsplit(str(base_url or "").strip())
    origin = normalize_origin(base_url)
    hostname = _ascii_hostname(parsed.hostname)
    if (
        not origin
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise UnsafeOutboundURLError("trusted service base URL is invalid")
    return OutboundURLPolicy(
        allowed_hosts=frozenset({hostname}),
        allowed_private_origins=frozenset({origin}),
        max_redirects=0,
        max_response_bytes=max(1, int(max_response_bytes)),
        timeout_seconds=max(0.1, float(timeout_seconds)),
        allowed_response_content_types=allowed_response_content_types,
    )


def trusted_service_url(
    base_url: str,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Join a service-relative path without allowing it to replace the origin."""

    base = str(base_url or "").strip().rstrip("/")
    origin = normalize_origin(base)
    if not origin:
        raise UnsafeOutboundURLError("trusted service base URL is invalid")
    raw_path = str(path or "").strip()
    if not raw_path.startswith("/") or raw_path.startswith("//"):
        raise UnsafeOutboundURLError("trusted service path must be origin-relative")
    url = f"{base}{raw_path}"
    if normalize_origin(url) != origin:
        raise UnsafeOutboundURLError("trusted service path changed the configured origin")
    return str(httpx.URL(url, params=params)) if params is not None else url


@dataclass(frozen=True, slots=True)
class _PinnedDestination:
    logical_url: str
    request_url: str
    host_header: str
    sni_hostname: str | None


async def safe_http_request(
    client: httpx.AsyncClient,
    method: Literal["GET", "POST"],
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    content: bytes | None = None,
    json: Any | None = None,
    policy: OutboundURLPolicy,
) -> httpx.Response:
    """Issue a bounded request whose socket destination is the validated IP.

    Only GET and POST are exposed because those are the egress operations used
    by Agent Console.  Caller-provided credentials are never forwarded to a
    different origin.  POST redirects are rejected so webhook signatures and
    service credentials cannot be replayed to an attacker-controlled target.
    """

    normalized_method = method.upper()
    if normalized_method not in {"GET", "POST"}:  # pragma: no cover - type guard
        raise ValueError("safe HTTP egress only supports GET and POST")
    if content is not None and json is not None:
        raise ValueError("safe HTTP egress accepts either content or json, not both")

    initial_url = str(url or "").strip()
    initial_origin = normalize_origin(initial_url)
    if not initial_origin:
        raise UnsafeOutboundURLError("outbound URL has an invalid origin")

    try:
        async with asyncio.timeout(max(0.1, float(policy.timeout_seconds))):
            return await _request_with_redirects(
                client,
                normalized_method,
                initial_url,
                initial_origin=initial_origin,
                headers=headers,
                content=content,
                json=json,
                policy=policy,
            )
    except TimeoutError as exc:
        raise httpx.TimeoutException("outbound request timed out") from exc


async def safe_trusted_service_request(
    client: httpx.AsyncClient,
    method: Literal["GET", "POST"],
    base_url: str,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    content: bytes | None = None,
    json: Any | None = None,
    timeout_seconds: float,
    max_response_bytes: int,
    allowed_response_content_types: tuple[str, ...],
) -> httpx.Response:
    """Call one fixed configured service origin through the pinned transport."""

    policy = trusted_service_policy(
        base_url,
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
        allowed_response_content_types=allowed_response_content_types,
    )
    return await safe_http_request(
        client,
        method,
        trusted_service_url(base_url, path, params=params),
        headers=headers,
        content=content,
        json=json,
        policy=policy,
    )


@asynccontextmanager
async def safe_trusted_service_stream(
    client: httpx.AsyncClient,
    base_url: str,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float,
    max_response_bytes: int,
    allowed_response_content_types: tuple[str, ...] = ("text/event-stream",),
) -> AsyncIterator[httpx.Response]:
    """Open a bounded GET stream to one exact configured service origin."""

    policy = trusted_service_policy(
        base_url,
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
        allowed_response_content_types=allowed_response_content_types,
    )
    logical_url = trusted_service_url(base_url, path, params=params)
    try:
        async with asyncio.timeout(policy.timeout_seconds):
            pinned = await _pin_destination(client, logical_url, policy=policy)
            request_headers = {
                str(key): str(value)
                for key, value in (headers or {}).items()
                if str(key).lower() != "host"
            }
            request_headers["Host"] = pinned.host_header
            request_headers["Connection"] = "close"
            timeout = policy.timeout_seconds
            extensions: dict[str, object] = {
                "timeout": {
                    "connect": timeout,
                    "read": timeout,
                    "write": timeout,
                    "pool": timeout,
                }
            }
            if pinned.sni_hostname:
                extensions["sni_hostname"] = pinned.sni_hostname
            request = httpx.Request(
                "GET",
                pinned.request_url,
                headers=request_headers,
                extensions=extensions,
            )
            async with _isolated_transport(client) as transport:
                upstream = await transport.handle_async_request(request)
                upstream.request = request
                if upstream.status_code in _REDIRECT_STATUSES:
                    await upstream.aclose()
                    raise UnsafeOutboundURLError(
                        "trusted service stream redirects are blocked"
                    )
                byte_limit = policy.max_response_bytes
                _validate_content_length(upstream, byte_limit=byte_limit)
                if 200 <= upstream.status_code < 300:
                    _validate_stream_content_type(
                        upstream,
                        allowed=policy.allowed_response_content_types,
                    )
                bounded_stream = _BoundedAsyncByteStream(
                    cast(httpx.AsyncByteStream, upstream.stream),
                    byte_limit=byte_limit,
                )
                response = httpx.Response(
                    status_code=upstream.status_code,
                    headers=upstream.headers,
                    stream=bounded_stream,
                    request=httpx.Request("GET", pinned.logical_url),
                    extensions=upstream.extensions,
                )
                try:
                    yield response
                finally:
                    await response.aclose()
                    if not upstream.is_closed:
                        await upstream.aclose()
    except TimeoutError as exc:
        raise httpx.TimeoutException("outbound stream timed out") from exc


async def _request_with_redirects(
    client: httpx.AsyncClient,
    method: str,
    initial_url: str,
    *,
    initial_origin: str,
    headers: Mapping[str, str] | None,
    content: bytes | None,
    json: Any | None,
    policy: OutboundURLPolicy,
) -> httpx.Response:
    """Run redirects inside the caller's single end-to-end deadline."""

    current_url = initial_url
    for redirect_count in range(policy.max_redirects + 1):
        pinned = await _pin_destination(client, current_url, policy=policy)
        request_headers = {
            str(key): str(value)
            for key, value in (headers or {}).items()
            if str(key).lower() != "host"
        }
        if normalize_origin(current_url) != initial_origin:
            request_headers = {
                key: value
                for key, value in request_headers.items()
                if key.lower() in {"accept", "accept-encoding", "user-agent"}
            }
        request_headers["Host"] = pinned.host_header
        # A fresh connection per request prevents a connection pinned for one
        # logical hostname from being reused for another hostname sharing an IP.
        request_headers["Connection"] = "close"

        response = await _bounded_request(
            client,
            method,
            pinned,
            headers=request_headers,
            content=content,
            json=json,
            policy=policy,
        )
        if response.status_code not in _REDIRECT_STATUSES:
            return response

        if method != "GET":
            raise UnsafeOutboundURLError("outbound POST redirects are blocked")
        location = str(response.headers.get("location") or "").strip()
        if not location:
            raise UnsafeOutboundURLError("outbound redirect is missing a location")
        if redirect_count >= policy.max_redirects:
            raise UnsafeOutboundURLError("outbound URL exceeded redirect limit")
        current_url = urljoin(current_url, location)

    raise UnsafeOutboundURLError("outbound URL exceeded redirect limit")


async def _pin_destination(
    client: httpx.AsyncClient,
    url: str,
    *,
    policy: OutboundURLPolicy,
) -> _PinnedDestination:
    """Validate syntax/policy, resolve once, then return a numeric request URL."""

    logical_url = await validate_outbound_url(url, policy=policy, resolve_dns=False)
    parsed = urlsplit(logical_url)
    hostname = _ascii_hostname(parsed.hostname)
    if not hostname or "%" in hostname:
        raise UnsafeOutboundURLError("outbound URL contains an invalid hostname")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:  # also guarded by validate_outbound_url
        raise UnsafeOutboundURLError("outbound URL contains an invalid port") from exc

    # MockTransport cannot make a network connection and is used by focused
    # tests.  Keep its logical URL so responders remain readable; real and
    # custom transports always receive a numeric, validated destination.
    if isinstance(getattr(client, "_transport", None), httpx.MockTransport):
        return _PinnedDestination(
            logical_url=logical_url,
            request_url=logical_url,
            host_header=_host_header(hostname, port, parsed.scheme),
            sni_hostname=hostname if parsed.scheme.lower() == "https" else None,
        )

    addresses = await _resolve_addresses(hostname, port, timeout=policy.timeout_seconds)
    allow_private = normalize_origin(logical_url) in policy.allowed_private_origins
    for address in addresses:
        _validate_address(address, allow_private=allow_private)

    # Sorting makes selection deterministic while still rejecting the entire
    # answer set when even one record points at a forbidden network.
    selected = sorted(addresses, key=lambda item: (item.version, int(item)))[0]
    numeric_host = f"[{selected.compressed}]" if selected.version == 6 else selected.compressed
    request_netloc = f"{numeric_host}:{port}"
    request_url = urlunsplit(
        (parsed.scheme, request_netloc, parsed.path, parsed.query, parsed.fragment)
    )
    return _PinnedDestination(
        logical_url=logical_url,
        request_url=request_url,
        host_header=_host_header(hostname, port, parsed.scheme),
        sni_hostname=hostname if parsed.scheme.lower() == "https" else None,
    )


async def _resolve_addresses(
    hostname: str,
    port: int,
    *,
    timeout: float,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    literal = _parse_ip(hostname)
    if literal is not None:
        return (literal,)
    try:
        records = await asyncio.wait_for(
            asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
            ),
            timeout=max(0.1, float(timeout)),
        )
    except TimeoutError as exc:
        raise httpx.ConnectTimeout("outbound DNS resolution timed out") from exc
    except socket.gaierror as exc:
        raise UnsafeOutboundURLError("outbound hostname could not be resolved") from exc

    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for record in records:
        if not record or len(record) < 5 or not record[4]:
            continue
        raw_address = str(record[4][0]).split("%", 1)[0]
        parsed = _parse_ip(raw_address)
        if parsed is None:
            raise UnsafeOutboundURLError("outbound DNS returned an invalid address")
        addresses.add(parsed)
    if not addresses:
        raise UnsafeOutboundURLError("outbound hostname resolved to no addresses")
    return tuple(addresses)


def _validate_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_private: bool,
) -> None:
    effective: ipaddress.IPv4Address | ipaddress.IPv6Address = address
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        effective = address.ipv4_mapped

    # Link-local ranges include the standard cloud metadata endpoint.  These
    # remain forbidden even for explicitly configured private service origins.
    if effective in _CLOUD_METADATA_ADDRESSES:
        raise UnsafeOutboundURLError("outbound URL resolves to a metadata address")
    if (
        effective.is_link_local
        or effective.is_multicast
        or effective.is_unspecified
    ):
        raise UnsafeOutboundURLError("outbound URL resolves to a forbidden address")
    if effective.is_loopback:
        if allow_private:
            return
        raise UnsafeOutboundURLError("outbound URL resolves to a non-public address")
    if effective.is_reserved:
        raise UnsafeOutboundURLError("outbound URL resolves to a forbidden address")
    if not allow_private and not effective.is_global:
        raise UnsafeOutboundURLError("outbound URL resolves to a non-public address")


async def _bounded_request(
    client: httpx.AsyncClient,
    method: str,
    destination: _PinnedDestination,
    *,
    headers: Mapping[str, str],
    content: bytes | None,
    json: Any | None,
    policy: OutboundURLPolicy,
) -> httpx.Response:
    timeout = max(0.1, float(policy.timeout_seconds))
    timeout_extension = {
        "connect": timeout,
        "read": timeout,
        "write": timeout,
        "pool": timeout,
    }
    extensions: dict[str, object] = {"timeout": timeout_extension}
    if destination.sni_hostname:
        extensions["sni_hostname"] = destination.sni_hostname

    request = httpx.Request(
        method,
        destination.request_url,
        headers=headers,
        content=content,
        json=json,
        extensions=extensions,
    )
    try:
        async with asyncio.timeout(timeout):
            async with _isolated_transport(client) as transport:
                # Calling the transport directly keeps httpx's access logger
                # from serializing sensitive query parameters (some upstream
                # APIs still require credentials in the query string). The
                # safe egress layer owns redirects, cookies and authentication.
                response = await transport.handle_async_request(request)
                response.request = request
                try:
                    if response.status_code in _REDIRECT_STATUSES:
                        return _detached_response(
                            response,
                            destination.logical_url,
                            b"",
                        )
                    byte_limit = max(1, int(policy.max_response_bytes))
                    _validate_content_length(response, byte_limit=byte_limit)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > byte_limit:
                            raise UnsafeOutboundResponseError(
                                f"outbound response exceeds {byte_limit} bytes"
                            )
                    payload = bytes(body)
                    _validate_content_type(
                        response,
                        payload,
                        allowed=policy.allowed_response_content_types,
                    )
                    return _detached_response(
                        response,
                        destination.logical_url,
                        payload,
                    )
                finally:
                    await response.aclose()
    except TimeoutError as exc:
        raise httpx.TimeoutException("outbound request timed out") from exc


@asynccontextmanager
async def _isolated_transport(
    client: httpx.AsyncClient,
) -> AsyncIterator[httpx.AsyncBaseTransport]:
    """Use a one-request pool for the standard network transport.

    A custom/Mock transport is caller-owned and retained for deterministic
    adapters.  The standard transport gets a new pool so no connection keyed by
    a numeric IP can cross logical host boundaries.
    """

    caller_transport = getattr(client, "_transport", None)
    if isinstance(caller_transport, httpx.AsyncHTTPTransport):
        transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        try:
            yield transport
        finally:
            await transport.aclose()
        return
    if not isinstance(caller_transport, httpx.AsyncBaseTransport):
        raise UnsafeOutboundURLError("outbound client has no async transport")
    yield caller_transport


class _BoundedAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, *, byte_limit: int) -> None:
        self._stream = stream
        self._byte_limit = max(1, int(byte_limit))
        self._received = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            self._received += len(chunk)
            if self._received > self._byte_limit:
                raise UnsafeOutboundResponseError(
                    f"outbound response exceeds {self._byte_limit} bytes"
                )
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


def _validate_content_length(response: httpx.Response, *, byte_limit: int) -> None:
    raw = response.headers.get("content-length")
    if not raw:
        return
    try:
        declared = int(raw)
    except ValueError as exc:
        raise UnsafeOutboundResponseError(
            "outbound response has invalid content-length"
        ) from exc
    if declared < 0 or declared > byte_limit:
        raise UnsafeOutboundResponseError(
            f"outbound response exceeds {byte_limit} bytes"
        )


def _validate_stream_content_type(
    response: httpx.Response,
    *,
    allowed: tuple[str, ...],
) -> None:
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    normalized_allowed = tuple(
        str(item or "").split(";", 1)[0].strip().lower()
        for item in allowed
        if str(item or "").strip()
    )
    if not media_type or not any(
        media_type.startswith(item) if item.endswith("/") else media_type == item
        for item in normalized_allowed
    ):
        raise UnsafeOutboundResponseError(
            "outbound response content-type is not allowed"
        )


def _validate_content_type(
    response: httpx.Response,
    content: bytes,
    *,
    allowed: tuple[str, ...],
) -> None:
    if not content:
        return
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    normalized_allowed = tuple(
        str(item or "").split(";", 1)[0].strip().lower()
        for item in allowed
        if str(item or "").strip()
    )
    if not media_type or not any(
        media_type.startswith(item) if item.endswith("/") else media_type == item
        for item in normalized_allowed
    ):
        raise UnsafeOutboundResponseError(
            "outbound response content-type is not allowed"
        )


def _detached_response(
    response: httpx.Response,
    logical_url: str,
    content: bytes,
) -> httpx.Response:
    return httpx.Response(
        status_code=response.status_code,
        headers=response.headers,
        content=content,
        request=httpx.Request(response.request.method, logical_url),
        extensions=response.extensions,
    )


def _ascii_hostname(value: str | None) -> str:
    hostname = str(value or "").strip().lower().rstrip(".")
    if not hostname:
        return ""
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def _host_header(hostname: str, port: int, scheme: str) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme.lower() == "https" else 80
    return host if port == default_port else f"{host}:{port}"


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None
