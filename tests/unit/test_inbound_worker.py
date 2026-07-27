from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

from app.bus.base import BusMessage, PermanentMessageError
from app.common.config import Settings
from app.common.types import InboundEvent
from app.container import InboundContainer, OutboundContainer
from app.orchestrator.outcome import (
    PermanentProcessingError,
    ProcessingOutcome,
    RetryableProcessingError,
)
from app.workers.inbound_worker import InboundWorker, _event_age_seconds, run_inbound_worker
from app.workers.outbound_worker import run_outbound_worker


class _CapturingBus:
    def __init__(self) -> None:
        self.consume_args: tuple[str, str, str, int, int] | None = None
        self.dlq: list[tuple[BusMessage, str]] = []

    async def ensure_group(self, stream: str, group: str) -> None:
        _ = (stream, group)

    async def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler,
        *,
        batch_size: int = 16,
        block_ms: int = 5_000,
    ):
        _ = handler
        self.consume_args = (stream, group, consumer, batch_size, block_ms)
        if False:
            yield None
        return

    async def move_to_dlq(self, message: BusMessage, reason: str) -> None:
        self.dlq.append((message, reason))


class _NoopOrchestrator:
    async def handle(self, event) -> None:
        _ = event


class _OutcomeOrchestrator:
    def __init__(self, result=None, *, error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    async def handle(self, event):
        _ = event
        if self.error is not None:
            raise self.error
        return self.result


def _inbound_message(message_id: str = "outcome-m1") -> BusMessage:
    return BusMessage(
        id="10-0",
        stream="cs:inbound",
        payload={
            "tenant_id": "demo",
            "channel": "web",
            "message_id": message_id,
            "session_id": "outcome-session",
            "user_id": "u1",
            "message": {"type": "text", "content": "hello"},
            "trace_id": "trace-outcome",
        },
        attempts=0,
    )


def test_event_age_uses_ingress_timestamp_and_clamps_clock_skew() -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    payload = dict(_inbound_message().payload)
    payload["received_at"] = now - timedelta(seconds=2.5)
    event = InboundEvent.model_validate(payload)
    assert _event_age_seconds(event, now=now) == 2.5

    payload["received_at"] = now + timedelta(seconds=1)
    future_event = InboundEvent.model_validate(payload)
    assert _event_age_seconds(future_event, now=now) == 0


class _MemoryPlugin:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.claimed_per_call: list[int] = []
        self.yield_before_claim = False

    async def drain_extraction_jobs(self, **kwargs) -> dict[str, int]:
        self.calls.append(kwargs)
        if self.yield_before_claim:
            await asyncio.sleep(0)
        claimed = self.claimed_per_call.pop(0) if self.claimed_per_call else 0
        return {"claimed": claimed, "succeeded": claimed, "failed": 0, "dead": 0}


class _DrawPlugin:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.queue_calls: list[dict] = []

    async def recover_stale_tasks(self, **kwargs) -> dict[str, int]:
        self.calls.append(kwargs)
        return {"recovered": 0, "callbacks_sent": 0, "callback_failed": 0}

    async def drain_queued_tasks(self, **kwargs) -> dict[str, int]:
        self.queue_calls.append(kwargs)
        return {"claimed": 0, "completed": 0, "failed": 0, "auto_retried": 0}


class _WxbotPlugin:
    def __init__(self) -> None:
        self.observations = []
        self.drain_calls: list[dict] = []

    async def record_group_observation(self, event) -> bool:
        self.observations.append(event)
        return True

    async def drain_group_summary_jobs(self, **kwargs) -> dict[str, int]:
        self.drain_calls.append(kwargs)
        return {"claimed": 0, "succeeded": 0, "failed": 0}


class _InboundPluginRegistry:
    def __init__(
        self,
        plugins: dict[str, object],
        *,
        owner_allowed: bool = True,
        owner_error: Exception | None = None,
    ) -> None:
        self.loaded_plugins = plugins
        self.owner_allowed = owner_allowed
        self.owner_error = owner_error
        self.gate_calls: list[tuple[str, str, str]] = []

    async def execution_allowed(self, owner: str, ctx) -> bool:
        self.gate_calls.append(
            (owner, ctx.event.tenant_id, ctx.event.session_id)
        )
        if self.owner_error is not None:
            raise self.owner_error
        return self.owner_allowed


class _RegistryOrchestrator(_NoopOrchestrator):
    def __init__(
        self,
        memory_plugin: _MemoryPlugin | None = None,
        *,
        draw_plugin: _DrawPlugin | None = None,
        wxbot_plugin: _WxbotPlugin | None = None,
        owner_allowed: bool = True,
        owner_error: Exception | None = None,
    ) -> None:
        plugins = {}
        if memory_plugin is not None:
            plugins["memory"] = memory_plugin
        if draw_plugin is not None:
            plugins["draw"] = draw_plugin
        if wxbot_plugin is not None:
            plugins["wxbot"] = wxbot_plugin
        self.plugin_registry = _InboundPluginRegistry(
            plugins,
            owner_allowed=owner_allowed,
            owner_error=owner_error,
        )
        self.handled_events: list[InboundEvent] = []

    async def handle(self, event: InboundEvent) -> None:
        self.handled_events.append(event)


def _wxbot_group_message(message_id: str) -> BusMessage:
    payload = dict(_inbound_message(message_id).payload)
    payload.update(
        {
            "channel": "wechat",
            "session_id": "room@chatroom",
            "trace_id": f"trace-{message_id}",
        }
    )
    return BusMessage(
        id="20-0",
        stream="cs:inbound",
        payload=payload,
        attempts=0,
    )


@pytest.mark.asyncio
async def test_inbound_worker_skips_wxbot_observation_when_scope_owner_is_disabled() -> None:
    settings = Settings(
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    wxbot = _WxbotPlugin()
    orchestrator = _RegistryOrchestrator(
        wxbot_plugin=wxbot,
        owner_allowed=False,
    )
    bus = _CapturingBus()
    worker = InboundWorker(
        bus,
        orchestrator,
        settings,
    )  # type: ignore[arg-type]

    await worker._handle(_wxbot_group_message("scope-disabled-m1"))

    assert wxbot.observations == []
    assert orchestrator.plugin_registry.gate_calls == [
        ("wxbot", "demo", "room@chatroom")
    ]
    assert [event.message_id for event in orchestrator.handled_events] == [
        "scope-disabled-m1"
    ]
    assert bus.dlq == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_error", "expected_reason"),
    [
        (ConnectionError("plugin scope store unavailable"), "owner_gate_error"),
        (TimeoutError("plugin scope store timed out"), "owner_gate_timeout"),
    ],
)
async def test_inbound_worker_retries_when_wxbot_owner_gate_store_fails(
    owner_error: Exception,
    expected_reason: str,
) -> None:
    settings = Settings(
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    wxbot = _WxbotPlugin()
    orchestrator = _RegistryOrchestrator(
        wxbot_plugin=wxbot,
        owner_error=owner_error,
    )
    bus = _CapturingBus()
    worker = InboundWorker(
        bus,
        orchestrator,
        settings,
    )  # type: ignore[arg-type]

    with pytest.raises(RetryableProcessingError) as exc:
        await worker._handle(_wxbot_group_message("scope-error-m1"))

    assert exc.value.reason == expected_reason
    assert exc.value.error_type == "InboundPluginOwnerGateUnavailable"
    assert str(owner_error) not in str(exc.value)
    assert wxbot.observations == []
    assert orchestrator.handled_events == []
    assert orchestrator.plugin_registry.gate_calls == [
        ("wxbot", "demo", "room@chatroom")
    ]
    assert bus.dlq == []


@pytest.mark.asyncio
async def test_inbound_worker_uses_configured_bus_consume_settings() -> None:
    settings = Settings(
        worker_instance_id="node-a-01",
        bus_consume_batch_size=6,
        bus_consume_block_ms=900,
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    bus = _CapturingBus()
    worker = InboundWorker(bus, _NoopOrchestrator(), settings)  # type: ignore[arg-type]

    await worker.run()

    assert bus.consume_args == (
        settings.bus_inbound_stream,
        settings.bus_consumer_group,
        "inbound-node-a-01",
        6,
        900,
    )


@pytest.mark.asyncio
async def test_inbound_worker_accepts_completed_outcome() -> None:
    settings = Settings(
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    bus = _CapturingBus()
    orchestrator = _OutcomeOrchestrator(
        ProcessingOutcome.completed(route_label="faq")
    )
    worker = InboundWorker(bus, orchestrator, settings)  # type: ignore[arg-type]

    await worker._handle(_inbound_message("completed-m1"))

    assert bus.dlq == []


@pytest.mark.asyncio
async def test_inbound_worker_accepts_intentional_business_silence() -> None:
    settings = Settings(
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    bus = _CapturingBus()
    orchestrator = _OutcomeOrchestrator(
        ProcessingOutcome.intentionally_suppressed(
            route_label="canned",
            reason="reply_policy_off",
        )
    )
    worker = InboundWorker(bus, orchestrator, settings)  # type: ignore[arg-type]

    await worker._handle(_inbound_message("suppressed-m1"))

    assert bus.dlq == []


@pytest.mark.asyncio
async def test_inbound_worker_raises_retryable_outcome_to_bus() -> None:
    settings = Settings(
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    bus = _CapturingBus()
    orchestrator = _OutcomeOrchestrator(
        ProcessingOutcome.retryable_failure(
            reason="database_unavailable",
            error_type="ConnectionError",
        )
    )
    worker = InboundWorker(bus, orchestrator, settings)  # type: ignore[arg-type]

    with pytest.raises(RetryableProcessingError, match="database_unavailable"):
        await worker._handle(_inbound_message("retryable-m1"))

    assert bus.dlq == []


@pytest.mark.asyncio
async def test_inbound_worker_persists_permanent_outcome_to_dlq() -> None:
    settings = Settings(
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    bus = _CapturingBus()
    orchestrator = _OutcomeOrchestrator(
        ProcessingOutcome.permanent_failure(
            reason="unsupported_payload_version",
            error_type="SchemaError",
        )
    )
    worker = InboundWorker(bus, orchestrator, settings)  # type: ignore[arg-type]
    message = _inbound_message("permanent-m1")

    with pytest.raises(
        PermanentMessageError,
        match="permanent:unsupported_payload_version",
    ):
        await worker._handle(message)

    assert bus.dlq == []


@pytest.mark.asyncio
async def test_inbound_worker_treats_legacy_exception_as_retryable() -> None:
    settings = Settings(
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    bus = _CapturingBus()
    orchestrator = _OutcomeOrchestrator(error=ConnectionError("redis down"))
    worker = InboundWorker(bus, orchestrator, settings)  # type: ignore[arg-type]

    with pytest.raises(ConnectionError, match="redis down"):
        await worker._handle(_inbound_message("exception-m1"))

    assert bus.dlq == []


@pytest.mark.asyncio
async def test_inbound_worker_persists_raised_permanent_failure_to_dlq() -> None:
    settings = Settings(
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    bus = _CapturingBus()
    orchestrator = _OutcomeOrchestrator(
        error=PermanentProcessingError(
            "invalid_domain_event",
            error_type="DomainValidationError",
        )
    )
    worker = InboundWorker(bus, orchestrator, settings)  # type: ignore[arg-type]
    message = _inbound_message("permanent-exception-m1")

    with pytest.raises(
        PermanentMessageError,
        match="permanent:invalid_domain_event",
    ):
        await worker._handle(message)

    assert bus.dlq == []


@pytest.mark.asyncio
async def test_inbound_worker_never_runs_scheduler_owned_plugin_jobs() -> None:
    settings = Settings(
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        memory_llm_extraction_enabled=True,
        memory_llm_extraction_job_enabled=True,
        memory_llm_extraction_job_drain_enabled=True,
        wxbot_group_summary_enabled=True,
        draw_task_recovery_enabled=True,
        draw_task_queue_worker_enabled=True,
    )
    bus = _CapturingBus()
    memory = _MemoryPlugin()
    draw = _DrawPlugin()
    wxbot = _WxbotPlugin()
    worker = InboundWorker(
        bus,
        _RegistryOrchestrator(
            memory,
            draw_plugin=draw,
            wxbot_plugin=wxbot,
        ),
        settings,
    )  # type: ignore[arg-type]

    await worker.run()

    assert bus.consume_args is not None
    assert memory.calls == []
    assert draw.calls == []
    assert draw.queue_calls == []
    assert wxbot.drain_calls == []

@pytest.mark.asyncio
async def test_inbound_worker_records_group_observation_before_session_lock_failure() -> None:
    class _LockFailingOrchestrator(_RegistryOrchestrator):
        async def handle(self, event) -> None:
            _ = event
            raise TimeoutError("session lock busy")

    settings = Settings(
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    wxbot_plugin = _WxbotPlugin()
    orchestrator = _LockFailingOrchestrator(wxbot_plugin=wxbot_plugin)
    worker = InboundWorker(
        _CapturingBus(),
        orchestrator,
        settings,
        consumer_name="inbound-test",
    )  # type: ignore[arg-type]
    msg = BusMessage(
        id="2-0",
        stream=settings.bus_inbound_stream,
        payload={
            "tenant_id": "demo",
            "channel": "wechat",
            "message_id": "group-m1",
            "session_id": "room@chatroom",
            "user_id": "wxid_a",
            "message": {"type": "text", "content": "先记住这条"},
            "trace_id": "trace-group-1",
        },
        attempts=1,
    )

    with pytest.raises(TimeoutError, match="session lock busy"):
        await worker._handle(msg)

    assert [event.message_id for event in wxbot_plugin.observations] == ["group-m1"]
    assert wxbot_plugin.drain_calls == []


@pytest.mark.asyncio
async def test_run_inbound_worker_passes_shutdown_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        worker_shutdown_timeout_seconds=9.5,
        worker_heartbeat_interval_seconds=4.25,
        worker_metrics_host="127.0.0.7",
        worker_metrics_port=9464,
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    container = object.__new__(InboundContainer)
    container.bus = object()
    container.orchestrator = object()
    seen: dict[str, bool | float | int | str] = {}

    async def fake_build_container(settings_arg: Settings | None = None) -> InboundContainer:
        assert settings_arg is settings
        return container

    async def fake_run_worker_process(
        worker_name: str,
        *,
        initialize,
        run,
        stop,
        container,
        shutdown_timeout_seconds: float,
        heartbeat,
        readiness_check,
        readiness_interval_seconds: float,
        metrics_host: str,
        metrics_port: int,
    ) -> None:
        _ = (
            initialize,
            run,
            stop,
            container,
            heartbeat,
            readiness_check,
            readiness_interval_seconds,
        )
        seen["worker_name"] = worker_name
        seen["timeout"] = shutdown_timeout_seconds
        seen["has_heartbeat"] = heartbeat is not None
        seen["has_readiness_check"] = callable(readiness_check)
        seen["readiness_interval"] = readiness_interval_seconds
        seen["metrics_host"] = metrics_host
        seen["metrics_port"] = metrics_port

    monkeypatch.setattr("app.common.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.main.build_container", fake_build_container)
    monkeypatch.setattr("app.workers.inbound_worker.run_worker_process", fake_run_worker_process)
    monkeypatch.delenv("APP_PROCESS_ROLE", raising=False)

    await run_inbound_worker()

    assert seen == {
        "worker_name": "inbound",
        "timeout": settings.worker_shutdown_timeout_seconds,
        "has_heartbeat": True,
        "has_readiness_check": True,
        "readiness_interval": settings.worker_heartbeat_interval_seconds,
        "metrics_host": settings.worker_metrics_host,
        "metrics_port": settings.worker_metrics_port,
    }
    assert "APP_PROCESS_ROLE" not in os.environ


@pytest.mark.asyncio
async def test_run_outbound_worker_passes_shutdown_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        worker_shutdown_timeout_seconds=7.25,
        worker_heartbeat_interval_seconds=3.75,
        worker_metrics_host="127.0.0.8",
        worker_metrics_port=9465,
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    container = object.__new__(OutboundContainer)
    container.dispatcher = object()
    container.outbox_relay = object()
    seen: dict[str, bool | float | int | str] = {}

    async def fake_build_container(settings_arg: Settings | None = None) -> OutboundContainer:
        assert settings_arg is settings
        return container

    async def fake_run_worker_process(
        worker_name: str,
        *,
        initialize,
        run,
        stop,
        container,
        shutdown_timeout_seconds: float,
        heartbeat,
        readiness_check,
        readiness_interval_seconds: float,
        metrics_host: str,
        metrics_port: int,
    ) -> None:
        _ = (
            initialize,
            run,
            stop,
            container,
            heartbeat,
            readiness_check,
            readiness_interval_seconds,
        )
        seen["worker_name"] = worker_name
        seen["timeout"] = shutdown_timeout_seconds
        seen["has_heartbeat"] = heartbeat is not None
        seen["has_readiness_check"] = callable(readiness_check)
        seen["readiness_interval"] = readiness_interval_seconds
        seen["metrics_host"] = metrics_host
        seen["metrics_port"] = metrics_port

    monkeypatch.setattr("app.common.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.main.build_container", fake_build_container)
    monkeypatch.setattr("app.workers.outbound_worker.run_worker_process", fake_run_worker_process)
    monkeypatch.setenv("APP_PROCESS_ROLE", "api")

    await run_outbound_worker()

    assert seen == {
        "worker_name": "outbound",
        "timeout": settings.worker_shutdown_timeout_seconds,
        "has_heartbeat": True,
        "has_readiness_check": True,
        "readiness_interval": settings.worker_heartbeat_interval_seconds,
        "metrics_host": settings.worker_metrics_host,
        "metrics_port": settings.worker_metrics_port,
    }
    assert os.environ["APP_PROCESS_ROLE"] == "api"
