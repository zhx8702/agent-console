"""
Unit tests for the inbound FastAPI router.

- Uses starlette/fastapi TestClient.
- Fakes Redis (via monkeypatch on app.infra.redis_client.get_redis) and the
  bus (via Container).
"""
from __future__ import annotations

import time

import orjson
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.common.config import get_settings
from app.common.hashing import hmac_sha256
from app.container import Container
from tests.unit._fakes import InMemoryBus, InMemoryRedis


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> InMemoryRedis:
    r = InMemoryRedis()
    # Patch the getter so both router.build_router and _check_idempotency
    # pick up the fake.
    monkeypatch.setattr("app.infra.redis_client.get_redis", lambda: r)
    monkeypatch.setattr("app.ingress.router.get_redis", lambda: r)
    return r


@pytest.fixture
def client_and_bus(fake_redis: InMemoryRedis) -> tuple[TestClient, InMemoryBus]:
    from app.ingress.router import build_router

    bus = InMemoryBus()
    container = Container(bus=bus)  # type: ignore[arg-type]
    app = FastAPI()
    app.include_router(build_router(container))
    return TestClient(app), bus


def _make_body(**overrides: object) -> bytes:
    body = {
        "message_id": "msg-abc",
        "tenant_id": "demo",
        "channel": "web",
        "user_id": "u1",
        "session_id": "se_test00000000000001",
        "message": {"type": "text", "content": "hello"},
    }
    body.update(overrides)  # type: ignore[arg-type]
    return orjson.dumps(body)


def _sign(body: bytes, secret: str) -> tuple[str, str]:
    ts = str(int(time.time()))
    sig = hmac_sha256(secret, body)
    return sig, ts


def test_successful_publish(client_and_bus: tuple[TestClient, InMemoryBus]) -> None:
    client, bus = client_and_bus
    s = get_settings()
    body = _make_body()
    sig, ts = _sign(body, s.get_tenant_secret("demo") or "")
    resp = client.post(
        "/v1/webhook/inbound",
        content=body,
        headers={
            "X-Tenant-Id": "demo",
            "X-Signature": sig,
            "X-Timestamp": ts,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["trace_id"].startswith("tr_")

    # Bus received one message on the inbound stream.
    published = bus.streams[s.bus_inbound_stream]
    assert len(published) == 1
    msg = published[0]
    assert msg.payload["message_id"] == "msg-abc"
    assert msg.payload["tenant_id"] == "demo"
    assert msg.headers.get("partition_key") == "demo:se_test00000000000001"


def test_message_replay_fence_and_partition_are_scoped_by_connection(
    client_and_bus: tuple[TestClient, InMemoryBus],
) -> None:
    client, bus = client_and_bus
    settings = get_settings()

    for connection_id in ("feixin-primary", "feixin-backup"):
        body = _make_body(
            adapter_id="feixin-gateway",
            connection_id=connection_id,
            conversation_id=f"cnv_{connection_id}",
            external_message_id="msg-abc",
            external_conversation_id="shared-upstream-room",
            external_user_id="shared-upstream-user",
        )
        signature, timestamp = _sign(body, settings.get_tenant_secret("demo") or "")
        response = client.post(
            "/v1/webhook/inbound",
            content=body,
            headers={
                "X-Tenant-Id": "demo",
                "X-Signature": signature,
                "X-Timestamp": timestamp,
            },
        )
        assert response.status_code == 202, response.text

    published = bus.streams[settings.bus_inbound_stream]
    assert len(published) == 2
    assert {message.headers.get("connection_id") for message in published} == {
        "feixin-primary",
        "feixin-backup",
    }
    assert {message.headers.get("partition_key") for message in published} == {
        "demo:feixin-primary:se_test00000000000001",
        "demo:feixin-backup:se_test00000000000001",
    }


def test_legacy_and_managed_replay_fence_namespaces_cannot_collide() -> None:
    from app.ingress.router import _idempotency_key

    assert _idempotency_key("tenant:connection", "same:message") != _idempotency_key(
        "tenant",
        "same:message",
        "connection",
    )


def test_signed_tenant_is_injected_when_payload_omits_it(
    client_and_bus: tuple[TestClient, InMemoryBus],
) -> None:
    client, bus = client_and_bus
    settings = get_settings()
    body = _make_body()
    raw = orjson.loads(body)
    raw.pop("tenant_id")
    body = orjson.dumps(raw)
    sig, ts = _sign(body, settings.get_tenant_secret("demo") or "")

    resp = client.post(
        "/v1/webhook/inbound",
        content=body,
        headers={
            "X-Tenant-Id": "demo",
            "X-Signature": sig,
            "X-Timestamp": ts,
        },
    )

    assert resp.status_code == 202, resp.text
    published = bus.streams[settings.bus_inbound_stream]
    assert published[0].payload["tenant_id"] == "demo"


@pytest.mark.parametrize("payload_tenant_id", ["other-tenant", "", None])
def test_payload_cannot_override_signed_tenant(
    client_and_bus: tuple[TestClient, InMemoryBus],
    payload_tenant_id: object,
) -> None:
    client, bus = client_and_bus
    settings = get_settings()
    body = _make_body(tenant_id=payload_tenant_id)
    sig, ts = _sign(body, settings.get_tenant_secret("demo") or "")

    resp = client.post(
        "/v1/webhook/inbound",
        content=body,
        headers={
            "X-Tenant-Id": "demo",
            "X-Signature": sig,
            "X-Timestamp": ts,
        },
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "tenant_id_mismatch"
    assert settings.bus_inbound_stream not in bus.streams


def test_json_body_must_be_an_object(
    client_and_bus: tuple[TestClient, InMemoryBus],
) -> None:
    client, _ = client_and_bus
    settings = get_settings()
    body = orjson.dumps(["not", "an", "event"])
    sig, ts = _sign(body, settings.get_tenant_secret("demo") or "")

    resp = client.post(
        "/v1/webhook/inbound",
        content=body,
        headers={
            "X-Tenant-Id": "demo",
            "X-Signature": sig,
            "X-Timestamp": ts,
        },
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_schema:object_required"


def test_oversized_session_id_is_rejected_before_bus_publish(
    client_and_bus: tuple[TestClient, InMemoryBus],
) -> None:
    client, bus = client_and_bus
    settings = get_settings()
    body = _make_body(session_id="s" * 257)
    sig, ts = _sign(body, settings.get_tenant_secret("demo") or "")

    resp = client.post(
        "/v1/webhook/inbound",
        content=body,
        headers={
            "X-Tenant-Id": "demo",
            "X-Signature": sig,
            "X-Timestamp": ts,
        },
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_schema:1"
    assert settings.bus_inbound_stream not in bus.streams


@pytest.mark.parametrize(
    "overrides",
    [
        {"sesion_id": "misspelled"},
        {"message": {"type": "text", "content": "hello", "contnet": "typo"}},
        {
            "message": {
                "type": "text",
                "content": "hello",
                "attachments": [
                    {"type": "image", "url": "https://example.test/a", "urll": "typo"}
                ],
            }
        },
    ],
)
def test_unknown_fields_are_rejected_before_bus_publish(
    client_and_bus: tuple[TestClient, InMemoryBus],
    overrides: dict[str, object],
) -> None:
    client, bus = client_and_bus
    settings = get_settings()
    body = _make_body(**overrides)
    sig, ts = _sign(body, settings.get_tenant_secret("demo") or "")

    resp = client.post(
        "/v1/webhook/inbound",
        content=body,
        headers={
            "X-Tenant-Id": "demo",
            "X-Signature": sig,
            "X-Timestamp": ts,
        },
    )

    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("invalid_schema:")
    assert settings.bus_inbound_stream not in bus.streams


def test_missing_signature_returns_401(
    client_and_bus: tuple[TestClient, InMemoryBus],
) -> None:
    client, _ = client_and_bus
    body = _make_body()
    resp = client.post(
        "/v1/webhook/inbound",
        content=body,
        headers={"X-Tenant-Id": "demo"},
    )
    assert resp.status_code == 401


def test_bad_signature_returns_401(
    client_and_bus: tuple[TestClient, InMemoryBus],
) -> None:
    client, _ = client_and_bus
    body = _make_body()
    ts = str(int(time.time()))
    resp = client.post(
        "/v1/webhook/inbound",
        content=body,
        headers={
            "X-Tenant-Id": "demo",
            "X-Signature": "deadbeef",
            "X-Timestamp": ts,
        },
    )
    assert resp.status_code == 401


def test_stale_timestamp_returns_401(
    client_and_bus: tuple[TestClient, InMemoryBus],
) -> None:
    client, _ = client_and_bus
    s = get_settings()
    body = _make_body()
    secret = s.get_tenant_secret("demo") or ""
    old_ts = str(int(time.time()) - (s.inbound_signature_window_seconds + 120))
    sig = hmac_sha256(secret, body)
    resp = client.post(
        "/v1/webhook/inbound",
        content=body,
        headers={
            "X-Tenant-Id": "demo",
            "X-Signature": sig,
            "X-Timestamp": old_ts,
        },
    )
    assert resp.status_code == 401


def test_duplicate_replay_returns_duplicate(
    client_and_bus: tuple[TestClient, InMemoryBus],
) -> None:
    client, bus = client_and_bus
    s = get_settings()
    secret = s.get_tenant_secret("demo") or ""

    body = _make_body(message_id="dup-1")
    sig, ts = _sign(body, secret)
    headers = {"X-Tenant-Id": "demo", "X-Signature": sig, "X-Timestamp": ts}

    r1 = client.post("/v1/webhook/inbound", content=body, headers=headers)
    assert r1.status_code == 202

    # Re-sign the same body with a fresh timestamp; message_id still matches.
    sig2, ts2 = _sign(body, secret)
    headers2 = {"X-Tenant-Id": "demo", "X-Signature": sig2, "X-Timestamp": ts2}
    r2 = client.post("/v1/webhook/inbound", content=body, headers=headers2)
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate"

    # Only one message on the bus.
    assert len(bus.streams[s.bus_inbound_stream]) == 1


def test_body_too_large_returns_413(
    client_and_bus: tuple[TestClient, InMemoryBus],
) -> None:
    client, _ = client_and_bus
    s = get_settings()
    # Build a body just above the configured max. 1MB default -> ~1.1MB.
    big_content = "x" * (s.inbound_max_body_bytes + 1024)
    big_body = orjson.dumps({"message_id": "big-1", "tenant_id": "demo", "blob": big_content})
    secret = s.get_tenant_secret("demo") or ""
    sig, ts = _sign(big_body, secret)
    resp = client.post(
        "/v1/webhook/inbound",
        content=big_body,
        headers={"X-Tenant-Id": "demo", "X-Signature": sig, "X-Timestamp": ts},
    )
    assert resp.status_code == 413


def test_rate_limit_returns_429(
    client_and_bus: tuple[TestClient, InMemoryBus],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = client_and_bus
    s = get_settings()
    secret = s.get_tenant_secret("demo") or ""

    # Force the limiter to reject everything.
    from app.ingress import router as router_module

    async def always_deny(self, tenant_id: str, *, now_ms: int) -> bool:
        return False

    monkeypatch.setattr(
        router_module.TokenBucketRateLimiter, "allow", always_deny, raising=True
    )

    body = _make_body(message_id="rl-1")
    sig, ts = _sign(body, secret)
    resp = client.post(
        "/v1/webhook/inbound",
        content=body,
        headers={"X-Tenant-Id": "demo", "X-Signature": sig, "X-Timestamp": ts},
    )
    assert resp.status_code == 429


def test_invalid_json_returns_400(
    client_and_bus: tuple[TestClient, InMemoryBus],
) -> None:
    client, _ = client_and_bus
    s = get_settings()
    secret = s.get_tenant_secret("demo") or ""
    body = b"not json"
    sig, ts = _sign(body, secret)
    resp = client.post(
        "/v1/webhook/inbound",
        content=body,
        headers={"X-Tenant-Id": "demo", "X-Signature": sig, "X-Timestamp": ts},
    )
    assert resp.status_code == 400


def test_missing_tenant_returns_400(
    client_and_bus: tuple[TestClient, InMemoryBus],
) -> None:
    client, _ = client_and_bus
    body = _make_body()
    resp = client.post("/v1/webhook/inbound", content=body)
    assert resp.status_code == 400
