from __future__ import annotations

import importlib
import importlib.util
import io
import socket
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import urllib3

CLIENT_ROOT = Path(__file__).parents[2] / "wxbot_client"
sys.path.insert(0, str(CLIENT_ROOT))

safe_http = importlib.import_module("client.safe_http")


def _dns_records(*addresses: str) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))
        for address in addresses
    ]


class _Pool:
    def __init__(
        self,
        capture: dict[str, Any],
        *,
        response_body: bytes = b'{"ok":true,"session_token":"session-secret"}',
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.capture = capture
        self.response_body = response_body
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}

    def urlopen(self, method: str, target: str, **kwargs: Any) -> urllib3.HTTPResponse:
        self.capture["request"] = (method, target, kwargs)
        return urllib3.HTTPResponse(
            body=io.BytesIO(self.response_body),
            headers=self.headers,
            status=self.status,
            preload_content=False,
        )

    def close(self) -> None:
        self.capture["closed"] = True


def _install_pool(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response_body: bytes = b'{"ok":true,"session_token":"session-secret"}',
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    capture: dict[str, Any] = {}

    def pool_factory(**kwargs: Any) -> _Pool:
        capture["pool"] = kwargs
        return _Pool(
            capture,
            response_body=response_body,
            status=status,
            headers=headers,
        )

    monkeypatch.setattr(safe_http.urllib3, "HTTPSConnectionPool", pool_factory)
    return capture


def test_safe_json_post_pins_ip_and_preserves_logical_tls_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns_records("93.184.216.34"),
    )
    capture = _install_pool(monkeypatch)

    response = safe_http.safe_json_post(
        "https://AUTH.example:8443/",
        "/api/v1/session/login",
        {"device_token": "device-secret"},
        timeout_seconds=3,
        max_response_bytes=1024,
    )

    assert response.status_code == 200
    assert response.payload["session_token"] == "session-secret"
    assert capture["pool"]["host"] == "93.184.216.34"
    assert capture["pool"]["port"] == 8443
    assert capture["pool"]["assert_hostname"] == "auth.example"
    assert capture["pool"]["server_hostname"] == "auth.example"
    method, target, request = capture["request"]
    assert (method, target) == ("POST", "/api/v1/session/login")
    assert request["redirect"] is False
    assert request["retries"] is False
    assert request["headers"]["Host"] == "auth.example:8443"
    assert request["headers"]["Accept-Encoding"] == "identity"
    assert b"device-secret" in request["body"]
    assert capture["closed"] is True


def test_safe_json_post_rejects_mixed_public_and_private_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns_records("93.184.216.34", "127.0.0.1"),
    )
    opened = False

    def pool_factory(**_kwargs: Any) -> None:
        nonlocal opened
        opened = True

    monkeypatch.setattr(safe_http.urllib3, "HTTPSConnectionPool", pool_factory)

    with pytest.raises(safe_http.SafeHTTPError, match="non-public"):
        safe_http.safe_json_post(
            "https://auth.example",
            "/api/v1/session/login",
            {},
            timeout_seconds=1,
        )

    assert opened is False


@pytest.mark.parametrize(
    ("base_url", "path"),
    [
        ("http://auth.example", "/api/v1/session/login"),
        ("https://127.0.0.1", "/api/v1/session/login"),
        ("https://auth.example/hidden", "/api/v1/session/login"),
        ("https://user:secret@auth.example", "/api/v1/session/login"),
        ("https://auth.example", "//attacker.example/steal"),
        ("https://auth.example", "/login?next=https://attacker.example"),
    ],
)
def test_safe_json_post_rejects_unsafe_origins_and_paths(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    path: str,
) -> None:
    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns_records("93.184.216.34"),
    )

    with pytest.raises(safe_http.SafeHTTPError):
        safe_http.safe_json_post(base_url, path, {}, timeout_seconds=1)


@pytest.mark.parametrize(
    ("status", "body", "headers", "error"),
    [
        (302, b"", {"Location": "https://attacker.example"}, "redirects"),
        (
            200,
            b'{"ok":true}',
            {"Content-Type": "text/html"},
            "content-type",
        ),
        (
            200,
            b'{"ok":true}',
            {"Content-Type": "application/json", "Content-Length": "9999"},
            "too large",
        ),
    ],
)
def test_safe_json_post_blocks_redirects_and_untrusted_or_oversized_responses(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
    headers: dict[str, str],
    error: str,
) -> None:
    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns_records("93.184.216.34"),
    )
    _install_pool(
        monkeypatch,
        response_body=body,
        status=status,
        headers=headers,
    )

    with pytest.raises(safe_http.SafeHTTPError, match=error):
        safe_http.safe_json_post(
            "https://auth.example",
            "/api/v1/session/login",
            {},
            timeout_seconds=1,
            max_response_bytes=64,
        )


def test_auth_client_requires_explicit_device_binding_consent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    envelope_module = types.ModuleType("auth_envelope")
    envelope_module.AuthEnvelopeError = type("AuthEnvelopeError", (RuntimeError,), {})
    envelope_module.new_client_nonce = lambda: "nonce"
    verifier_module = types.ModuleType("client.auth_verifier")
    verifier_module.AuthEnvelopeVerifier = object
    machine_module = types.ModuleType("client.machine")
    machine_module.collect_machine_info = lambda _existing="": {}
    monkeypatch.setitem(sys.modules, "auth_envelope", envelope_module)
    monkeypatch.setitem(sys.modules, "client.auth_verifier", verifier_module)
    monkeypatch.setitem(sys.modules, "client.machine", machine_module)
    spec = importlib.util.spec_from_file_location(
        "_wxbot_auth_client_consent_under_test",
        CLIENT_ROOT / "client" / "auth_client.py",
    )
    assert spec is not None and spec.loader is not None
    auth_client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(auth_client)

    with pytest.raises(auth_client.AuthClientError, match="device binding consent is required"):
        auth_client.AuthClient(
            {
                "auth_base_url": "https://auth.example",
                "cache_path": str(tmp_path / "client_state.json"),
                "device_binding_consent": False,
            }
        )


def test_auth_client_does_not_expose_transport_or_upstream_error_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope_module = types.ModuleType("auth_envelope")
    envelope_module.AuthEnvelopeError = type("AuthEnvelopeError", (RuntimeError,), {})
    envelope_module.new_client_nonce = lambda: "nonce"
    verifier_module = types.ModuleType("client.auth_verifier")
    verifier_module.AuthEnvelopeVerifier = object
    machine_module = types.ModuleType("client.machine")
    machine_module.collect_machine_info = lambda: {}
    monkeypatch.setitem(sys.modules, "auth_envelope", envelope_module)
    monkeypatch.setitem(sys.modules, "client.auth_verifier", verifier_module)
    monkeypatch.setitem(sys.modules, "client.machine", machine_module)
    spec = importlib.util.spec_from_file_location(
        "_wxbot_auth_client_under_test",
        CLIENT_ROOT / "client" / "auth_client.py",
    )
    assert spec is not None and spec.loader is not None
    auth_client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(auth_client)

    client = auth_client.AuthClient.__new__(auth_client.AuthClient)
    client.base_url = "https://auth.example"
    client.timeout = 2

    def fail_request(*_args: Any, **_kwargs: Any) -> None:
        raise safe_http.SafeHTTPError("https://auth.example?token=transport-secret")

    monkeypatch.setattr(auth_client, "safe_json_post", fail_request)
    with pytest.raises(auth_client.AuthClientError) as transport_error:
        client._post("/api/v1/session/login", {"token": "request-secret"})
    assert str(transport_error.value) == "auth service request failed"
    assert transport_error.value.__cause__ is None

    monkeypatch.setattr(
        auth_client,
        "safe_json_post",
        lambda *_args, **_kwargs: safe_http.SafeJSONResponse(
            status_code=401,
            payload={"ok": False, "error": "upstream-secret"},
        ),
    )
    with pytest.raises(auth_client.AuthClientError) as upstream_error:
        client._post("/api/v1/session/login", {"token": "request-secret"})
    assert str(upstream_error.value) == "auth service rejected request (HTTP 401)"
    assert "secret" not in str(upstream_error.value)
