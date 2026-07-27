from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.bus.redis_streams import RedisStreamBus
from app.common.config import Settings
from app.common.exceptions import UpstreamUnavailable
from app.common.types import Channel, OutboundReply, ReplySegment, ReplyType
from app.egress.dispatcher import OutboundDispatcher
from app.orchestrator.effect_handlers import EffectHandlerRegistry
from app.workers.inbound_worker import InboundWorker
from tests.unit._fakes import InMemoryBus

ROOT = Path(__file__).resolve().parents[2]


def test_redis_stream_bus_uses_typed_reliability_settings() -> None:
    settings = Settings(
        _env_file=None,
        bus_max_attempts=9,
        bus_retry_base_seconds=1.5,
        bus_retry_max_seconds=17.0,
        bus_pending_claim_idle_ms=456_000,
    )

    bus = RedisStreamBus(SimpleNamespace(), settings)  # type: ignore[arg-type]

    assert bus._max_attempts == 9
    assert bus._retry_base_seconds == 1.5
    assert bus._retry_max_seconds == 17.0
    assert bus._pending_idle_ms == 456_000


def test_retry_backoff_caps_cannot_be_lower_than_their_base() -> None:
    with pytest.raises(ValidationError, match="bus_retry_max_seconds"):
        Settings(
            _env_file=None,
            bus_retry_base_seconds=5,
            bus_retry_max_seconds=4,
        )
    with pytest.raises(ValidationError, match="outbound_transport_retry_max_seconds"):
        Settings(
            _env_file=None,
            outbound_transport_retry_base_seconds=2,
            outbound_transport_retry_max_seconds=1,
        )


@pytest.mark.asyncio
async def test_outbound_transport_attempt_count_is_configurable() -> None:
    settings = Settings(
        _env_file=None,
        outbound_transport_max_attempts=2,
        outbound_transport_retry_base_seconds=0,
        outbound_transport_retry_max_seconds=0,
    )
    calls = 0

    def fail(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline")

    reply = OutboundReply(
        tenant_id="demo",
        channel=Channel.WEB,
        user_id="user-1",
        session_id="session-1",
        type=ReplyType.TEXT,
        segments=[ReplySegment(type=ReplyType.TEXT, content="hello")],
        trace_id="trace-1",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        dispatcher = OutboundDispatcher(
            http_client=client,
            bus=InMemoryBus(),
            settings=settings,
        )
        with pytest.raises(UpstreamUnavailable):
            await dispatcher.send(reply)

    assert calls == 2


def test_effect_intent_relay_has_its_own_typed_settings() -> None:
    settings = Settings(
        _env_file=None,
        effect_intent_relay_poll_interval_seconds=0.75,
        effect_intent_relay_batch_size=7,
        effect_intent_relay_lease_seconds=41,
        effect_intent_relay_handler_timeout_seconds=11,
        effect_intent_relay_max_attempts=6,
        outbox_relay_batch_size=99,
        outbox_relay_max_attempts=88,
    )
    orchestrator = SimpleNamespace(
        message_store=object(),
        flow_effect_handler_registry=EffectHandlerRegistry(),
    )

    worker = InboundWorker(
        SimpleNamespace(),  # type: ignore[arg-type]
        orchestrator,  # type: ignore[arg-type]
        settings,
        consumer_name="inbound-test",
    )
    relay = worker._effect_intent_relay

    assert relay is not None
    assert relay._poll_interval == 0.75
    assert relay._batch_size == 7
    assert relay._lease_seconds == 41
    assert relay._handler_timeout == 11
    assert relay._max_attempts == 6


def test_environment_template_and_compose_expose_reliability_contract() -> None:
    env_values = {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    expected = {
        "OUTBOUND_TRANSPORT_MAX_ATTEMPTS": "3",
        "OUTBOUND_TRANSPORT_RETRY_BASE_SECONDS": "0.2",
        "OUTBOUND_TRANSPORT_RETRY_MAX_SECONDS": "2",
        "OUTBOX_RELAY_MAX_ATTEMPTS": "12",
        "EFFECT_INTENT_RELAY_MAX_ATTEMPTS": "12",
        "BUS_MAX_ATTEMPTS": "5",
        "BUS_RETRY_BASE_SECONDS": "1",
        "BUS_RETRY_MAX_SECONDS": "30",
        "BUS_PENDING_CLAIM_IDLE_MS": "300000",
        "WXBOT_PREVIEW_WAIT_SECONDS": "30",
    }
    for name, value in expected.items():
        assert env_values[name] == value
        assert env_values[f"COMPOSE_{name}"] == value

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for name in expected:
        assert f"{name}: ${{COMPOSE_{name}:-" in compose
