"""Small synchronous egress boundary for the standalone wxbot client.

The desktop companion cannot import :mod:`app.egress.safe_http`: it is run
directly from ``wxbot_client`` and intentionally has a much smaller dependency
set than Agent Console.  This module keeps the same important invariants for
its fixed remote authentication service: HTTPS only, exact-origin paths, DNS
answer-set validation, numeric-IP connection pinning with the logical TLS
identity, no proxies or redirects, and bounded JSON responses.
"""

from __future__ import annotations

import ipaddress
import json
import queue
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import urllib3

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_JSON_CONTENT_TYPES = frozenset({"application/json", "application/problem+json"})
_CLOUD_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


class SafeHTTPError(RuntimeError):
    """Raised when the companion's outbound HTTP policy is not satisfied."""


@dataclass(frozen=True, slots=True)
class SafeJSONResponse:
    status_code: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _PinnedHTTPSDestination:
    address: str
    hostname: str
    port: int
    host_header: str
    target: str


def safe_json_post(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
    max_response_bytes: int = 1024 * 1024,
) -> SafeJSONResponse:
    """POST JSON to one configured HTTPS origin through a pinned connection."""

    timeout = max(0.1, float(timeout_seconds))
    byte_limit = max(1, int(max_response_bytes))
    deadline = time.monotonic() + timeout
    destination = _pin_https_destination(base_url, path, deadline=deadline)
    try:
        encoded_payload = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SafeHTTPError("outbound JSON payload is invalid") from exc

    pool = urllib3.HTTPSConnectionPool(
        host=destination.address,
        port=destination.port,
        maxsize=1,
        block=True,
        retries=False,
        cert_reqs=ssl.CERT_REQUIRED,
        assert_hostname=destination.hostname,
        server_hostname=destination.hostname,
    )
    response: urllib3.HTTPResponse | None = None
    try:
        remaining = _remaining_seconds(deadline)
        response = pool.urlopen(
            "POST",
            destination.target,
            body=encoded_payload,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "Content-Type": "application/json",
                "Host": destination.host_header,
            },
            redirect=False,
            retries=False,
            preload_content=False,
            timeout=urllib3.Timeout(
                total=remaining,
                connect=remaining,
                read=remaining,
            ),
        )
        if response.status in _REDIRECT_STATUSES:
            raise SafeHTTPError("outbound POST redirects are blocked")
        content = _read_bounded_response(
            response,
            deadline=deadline,
            byte_limit=byte_limit,
        )
        _validate_json_content_type(response, content)
        try:
            decoded = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SafeHTTPError("outbound response is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise SafeHTTPError("outbound JSON response is not an object")
        return SafeJSONResponse(status_code=int(response.status), payload=decoded)
    except SafeHTTPError:
        raise
    except (OSError, urllib3.exceptions.HTTPError) as exc:
        raise SafeHTTPError("outbound request failed") from exc
    finally:
        if response is not None:
            response.close()
        pool.close()


def _pin_https_destination(
    base_url: str,
    path: str,
    *,
    deadline: float,
) -> _PinnedHTTPSDestination:
    parsed = urlsplit(str(base_url or "").strip())
    hostname = _ascii_hostname(parsed.hostname)
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise SafeHTTPError("auth service URL has an invalid port") from exc
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SafeHTTPError("auth service URL must be an HTTPS origin")

    raw_path = str(path or "").strip()
    path_parts = urlsplit(raw_path)
    if (
        not raw_path.startswith("/")
        or raw_path.startswith("//")
        or path_parts.scheme
        or path_parts.netloc
        or path_parts.query
        or path_parts.fragment
    ):
        raise SafeHTTPError("auth service path must be origin-relative")

    addresses = _resolve_addresses(hostname, port, deadline=deadline)
    for address in addresses:
        _validate_public_address(address)
    selected = sorted(addresses, key=lambda item: (item.version, int(item)))[0]
    return _PinnedHTTPSDestination(
        address=selected.compressed,
        hostname=hostname,
        port=port,
        host_header=_host_header(hostname, port),
        target=raw_path,
    )


def _resolve_addresses(
    hostname: str,
    port: int,
    *,
    deadline: float,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    literal = _parse_ip(hostname)
    if literal is not None:
        return (literal,)

    outcomes: queue.Queue[object] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            outcomes.put(
                socket.getaddrinfo(
                    hostname,
                    port,
                    socket.AF_UNSPEC,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                )
            )
        except BaseException as exc:  # returned to the waiting caller
            outcomes.put(exc)

    threading.Thread(target=resolve, daemon=True, name="wxbot-auth-dns").start()
    try:
        outcome = outcomes.get(timeout=_remaining_seconds(deadline))
    except queue.Empty as exc:
        raise SafeHTTPError("outbound DNS resolution timed out") from exc
    if isinstance(outcome, BaseException):
        raise SafeHTTPError("outbound hostname could not be resolved") from outcome

    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for record in outcome:
        if not record or len(record) < 5 or not record[4]:
            continue
        address = _parse_ip(str(record[4][0]).split("%", 1)[0])
        if address is None:
            raise SafeHTTPError("outbound DNS returned an invalid address")
        addresses.add(address)
    if not addresses:
        raise SafeHTTPError("outbound hostname resolved to no addresses")
    return tuple(addresses)


def _validate_public_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> None:
    effective: ipaddress.IPv4Address | ipaddress.IPv6Address = address
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        effective = address.ipv4_mapped
    if effective in _CLOUD_METADATA_ADDRESSES:
        raise SafeHTTPError("outbound URL resolves to a metadata address")
    if (
        effective.is_link_local
        or effective.is_loopback
        or effective.is_multicast
        or effective.is_reserved
        or effective.is_unspecified
        or not effective.is_global
    ):
        raise SafeHTTPError("outbound URL resolves to a non-public address")


def _read_bounded_response(
    response: urllib3.HTTPResponse,
    *,
    deadline: float,
    byte_limit: int,
) -> bytes:
    raw_length = response.headers.get("content-length")
    if raw_length:
        try:
            declared_length = int(raw_length)
        except ValueError as exc:
            raise SafeHTTPError("outbound response has invalid content-length") from exc
        if declared_length < 0 or declared_length > byte_limit:
            raise SafeHTTPError("outbound response is too large")

    body = bytearray()
    while True:
        remaining = _remaining_seconds(deadline)
        _set_socket_timeout(response, remaining)
        chunk = response.read(
            min(64 * 1024, byte_limit + 1 - len(body)),
            decode_content=False,
            cache_content=False,
        )
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > byte_limit:
            raise SafeHTTPError("outbound response is too large")
    return bytes(body)


def _validate_json_content_type(response: urllib3.HTTPResponse, content: bytes) -> None:
    if not content:
        raise SafeHTTPError("outbound JSON response is empty")
    media_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media_type not in _JSON_CONTENT_TYPES:
        raise SafeHTTPError("outbound response content-type is not allowed")


def _set_socket_timeout(response: urllib3.HTTPResponse, timeout: float) -> None:
    connection = getattr(response, "connection", None) or getattr(response, "_connection", None)
    sock = getattr(connection, "sock", None)
    if sock is not None:
        sock.settimeout(timeout)


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SafeHTTPError("outbound request timed out")
    return remaining


def _ascii_hostname(value: str | None) -> str:
    hostname = str(value or "").strip().lower().rstrip(".")
    if not hostname or "%" in hostname:
        return ""
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def _host_header(hostname: str, port: int) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    return host if port == 443 else f"{host}:{port}"


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None
