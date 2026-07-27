"""Shared outbound URL validation for webhooks and downloaded media."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, replace
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
_IMAGE_RESPONSE_CONTENT_TYPES = ("image/",)
_WEBHOOK_RESPONSE_CONTENT_TYPES = (
    "application/json",
    "application/problem+json",
    "text/plain",
)


class UnsafeOutboundURLError(httpx.HTTPError):
    """Raised when an outbound URL violates the configured egress policy."""


class UnsafeOutboundResponseError(httpx.HTTPError):
    """Raised when an outbound response violates size or media-type limits."""


@dataclass(frozen=True)
class OutboundURLPolicy:
    require_https: bool = False
    allowed_hosts: frozenset[str] = frozenset()
    allowed_private_origins: frozenset[str] = frozenset()
    max_redirects: int = 3
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    allowed_response_content_types: tuple[str, ...] = ()


def split_allowed_hosts(value: object) -> frozenset[str]:
    return frozenset(
        _normalize_hostname(item)
        for item in str(value or "").split(",")
        if _normalize_hostname(item)
    )


def normalize_origin(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    return (
        f"{parsed.scheme.lower()}://{_normalize_hostname(parsed.hostname)}:"
        f"{port or default_port}"
    )


def configure_http_client(
    client: httpx.AsyncClient,
    *,
    allowed_private_origins: tuple[str, ...] | list[str] = (),
    allowed_hosts: frozenset[str] | set[str] = frozenset(),
    origin_headers: dict[str, dict[str, str]] | None = None,
) -> None:
    """Attach a URL policy used by shared image download helpers.

    httpx does not expose application metadata on a client, so a namespaced
    attribute is used deliberately.  Existing mocked clients remain compatible.
    """

    origins = {
        normalized
        for value in allowed_private_origins
        if (normalized := normalize_origin(value))
    }
    existing = getattr(client, "_agent_console_outbound_policy", None)
    if isinstance(existing, OutboundURLPolicy):
        origins.update(existing.allowed_private_origins)
        hosts = set(existing.allowed_hosts)
    else:
        hosts = set()
    hosts.update(_normalize_hostname(value) for value in allowed_hosts if value)
    extensible_client = cast(Any, client)
    extensible_client._agent_console_outbound_policy = OutboundURLPolicy(
        allowed_hosts=frozenset(hosts),
        allowed_private_origins=frozenset(origins),
    )
    if origin_headers:
        existing_headers = dict(
            getattr(client, "_agent_console_origin_headers", {}) or {}
        )
        for raw_origin, values in origin_headers.items():
            origin = normalize_origin(raw_origin)
            if origin:
                existing_headers[origin] = dict(values)
        extensible_client._agent_console_origin_headers = existing_headers


def client_outbound_policy(client: httpx.AsyncClient) -> OutboundURLPolicy:
    policy = getattr(client, "_agent_console_outbound_policy", None)
    return policy if isinstance(policy, OutboundURLPolicy) else OutboundURLPolicy()


async def validate_outbound_url(
    url: str,
    *,
    policy: OutboundURLPolicy | None = None,
    resolve_dns: bool = True,
) -> str:
    configured = policy or OutboundURLPolicy()
    parsed = urlsplit(str(url or "").strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeOutboundURLError("outbound URL must use http or https")
    if configured.require_https and scheme != "https":
        raise UnsafeOutboundURLError("outbound URL must use https")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeOutboundURLError("outbound URL must not contain credentials")
    if not parsed.hostname:
        raise UnsafeOutboundURLError("outbound URL must include a hostname")
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeOutboundURLError("outbound URL contains an invalid port") from exc

    hostname = _normalize_hostname(parsed.hostname)
    if configured.allowed_hosts and not _host_allowed(hostname, configured.allowed_hosts):
        raise UnsafeOutboundURLError("outbound hostname is not in the allowlist")

    origin = normalize_origin(url)
    allow_private = origin in configured.allowed_private_origins
    literal_ip = _parse_ip(hostname)
    if literal_ip is not None:
        _require_global_address(literal_ip, allow_private=allow_private)
        return parsed.geturl()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        if not allow_private:
            raise UnsafeOutboundURLError("localhost outbound URLs are blocked")
        return parsed.geturl()

    if resolve_dns:
        try:
            records = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise UnsafeOutboundURLError("outbound hostname could not be resolved") from exc
        addresses = {
            str(record[4][0]).split("%", 1)[0]
            for record in records
            if record and len(record) >= 5 and record[4]
        }
        if not addresses:
            raise UnsafeOutboundURLError("outbound hostname resolved to no addresses")
        for address in addresses:
            parsed_ip = _parse_ip(address)
            if parsed_ip is None:
                raise UnsafeOutboundURLError("outbound hostname returned an invalid address")
            _require_global_address(parsed_ip, allow_private=allow_private)
    return parsed.geturl()


async def safe_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    policy: OutboundURLPolicy | None = None,
    max_response_bytes: int | None = None,
    timeout_seconds: float | None = None,
    allowed_content_types: tuple[str, ...] | None = None,
) -> httpx.Response:
    """GET media through the DNS-pinned, redirect-revalidating transport."""

    # Local import avoids a module cycle: safe_http owns transport mechanics and
    # imports this module's policy/error types.
    from app.egress.safe_http import safe_http_request

    configured = policy or client_outbound_policy(client)
    effective_policy = replace(
        configured,
        max_response_bytes=(
            configured.max_response_bytes
            if max_response_bytes is None
            else max_response_bytes
        ),
        timeout_seconds=(
            configured.timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        ),
        allowed_response_content_types=(
            allowed_content_types
            or configured.allowed_response_content_types
            or _IMAGE_RESPONSE_CONTENT_TYPES
        ),
    )
    initial_url = str(url or "").strip()
    request_headers = dict(
        (getattr(client, "_agent_console_origin_headers", {}) or {}).get(
            normalize_origin(initial_url),
            {},
        )
    )
    request_headers.update(headers or {})
    return await safe_http_request(
        client,
        "GET",
        initial_url,
        headers=request_headers,
        policy=effective_policy,
    )


async def safe_post(
    client: httpx.AsyncClient,
    url: str,
    *,
    json: object,
    policy: OutboundURLPolicy,
) -> httpx.Response:
    """POST through the DNS-pinned transport; redirects remain forbidden."""

    from app.egress.safe_http import safe_http_request

    effective_policy = replace(
        policy,
        allowed_response_content_types=(
            policy.allowed_response_content_types or _WEBHOOK_RESPONSE_CONTENT_TYPES
        ),
    )
    return await safe_http_request(
        client,
        "POST",
        url,
        json=json,
        policy=effective_policy,
    )


def _normalize_hostname(value: object) -> str:
    hostname = str(value or "").strip().lower().rstrip(".")
    if not hostname:
        return ""
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def _host_allowed(hostname: str, allowed_hosts: frozenset[str]) -> bool:
    for allowed in allowed_hosts:
        normalized = _normalize_hostname(allowed)
        if not normalized:
            continue
        if normalized.startswith("*."):
            suffix = normalized[1:]
            if hostname.endswith(suffix) and hostname != suffix[1:]:
                return True
        elif secrets_compare_hostname(hostname, normalized):
            return True
    return False


def secrets_compare_hostname(left: str, right: str) -> bool:
    # Hostnames are not secrets, but a fixed equality helper keeps wildcard and
    # exact comparisons centralized and avoids accidental substring matching.
    return left == right


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _require_global_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_private: bool,
) -> None:
    if allow_private:
        return
    if not address.is_global:
        raise UnsafeOutboundURLError("outbound URL resolves to a non-public address")
