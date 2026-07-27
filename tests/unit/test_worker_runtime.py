from __future__ import annotations

import asyncio

import pytest

from app.container import Container
from app.workers import runtime
from app.workers.readiness import RoleReadiness


class _FakeBus:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class _FakeHTTPClient:
    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


class _BlockingWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stop_calls = 0
        self._release = asyncio.Event()

    async def run(self) -> None:
        self.started.set()
        await self._release.wait()

    async def stop(self) -> None:
        self.stop_calls += 1
        self._release.set()


class _RecordingHeartbeat:
    def __init__(self) -> None:
        self.states: list[tuple[str, str]] = []

    async def start(self) -> None:
        self.states.append(("starting", ""))

    async def mark_ready(self) -> None:
        self.states.append(("ready", ""))

    async def set_state(self, state: str, *, detail: str = "") -> None:
        self.states.append((state, detail))

    async def stop(self, *, final_state: str = "stopping", detail: str = "") -> None:
        self.states.append((final_state, detail))


@pytest.mark.asyncio
async def test_run_worker_process_handles_shutdown_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _FakeBus()
    http_client = _FakeHTTPClient()
    container = Container(bus=bus)
    container.__dict__["_http_client"] = http_client
    worker = _BlockingWorker()

    seen: dict[str, asyncio.Event | int] = {"cleanup_calls": 0}

    def fake_install(stop_event: asyncio.Event):
        seen["stop_event"] = stop_event

        def cleanup() -> None:
            seen["cleanup_calls"] = int(seen["cleanup_calls"]) + 1

        return cleanup

    close_counts = {"redis": 0, "db": 0}

    async def fake_close_redis() -> None:
        close_counts["redis"] += 1

    async def fake_dispose_engine() -> None:
        close_counts["db"] += 1

    monkeypatch.setattr(runtime, "_install_shutdown_handlers", fake_install)
    monkeypatch.setattr(runtime, "close_redis", fake_close_redis)
    monkeypatch.setattr(runtime, "dispose_engine", fake_dispose_engine)

    task = asyncio.create_task(
        runtime.run_worker_process(
            "inbound",
            run=worker.run,
            stop=worker.stop,
            container=container,
            shutdown_timeout_seconds=0.1,
        )
    )

    await worker.started.wait()
    stop_event = seen.get("stop_event")
    assert isinstance(stop_event, asyncio.Event)
    stop_event.set()
    await task

    assert worker.stop_calls == 1
    assert bus.closed == 1
    assert http_client.closed == 1
    assert close_counts == {"redis": 1, "db": 1}
    assert seen["cleanup_calls"] == 1


@pytest.mark.asyncio
async def test_run_worker_process_propagates_worker_error_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _FakeBus()
    http_client = _FakeHTTPClient()
    container = Container(bus=bus)
    container.__dict__["_http_client"] = http_client
    close_counts = {"redis": 0, "db": 0}

    def fake_install(stop_event: asyncio.Event):
        _ = stop_event
        return lambda: None

    async def fake_close_redis() -> None:
        close_counts["redis"] += 1

    async def fake_dispose_engine() -> None:
        close_counts["db"] += 1

    async def fail() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(runtime, "_install_shutdown_handlers", fake_install)
    monkeypatch.setattr(runtime, "close_redis", fake_close_redis)
    monkeypatch.setattr(runtime, "dispose_engine", fake_dispose_engine)

    with pytest.raises(RuntimeError, match="boom"):
        await runtime.run_worker_process(
            "outbound",
            run=fail,
            stop=None,
            container=container,
        )

    assert bus.closed == 1
    assert http_client.closed == 1
    assert close_counts == {"redis": 1, "db": 1}


@pytest.mark.asyncio
async def test_run_worker_process_stays_starting_while_initialization_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = Container(bus=_FakeBus())
    heartbeat = _RecordingHeartbeat()
    initialize_started = asyncio.Event()
    initialize_release = asyncio.Event()
    run_called = False
    seen: dict[str, asyncio.Event] = {}

    def fake_install(stop_event: asyncio.Event):
        seen["stop_event"] = stop_event
        return lambda: None

    async def initialize() -> None:
        initialize_started.set()
        await initialize_release.wait()

    async def run() -> None:
        nonlocal run_called
        run_called = True

    async def noop() -> None:
        return None

    monkeypatch.setattr(runtime, "_install_shutdown_handlers", fake_install)
    monkeypatch.setattr(runtime, "close_redis", noop)
    monkeypatch.setattr(runtime, "dispose_engine", noop)

    task = asyncio.create_task(
        runtime.run_worker_process(
            "inbound",
            initialize=initialize,
            run=run,
            stop=None,
            container=container,
            heartbeat=heartbeat,  # type: ignore[arg-type]
        )
    )
    await initialize_started.wait()

    assert heartbeat.states == [("starting", "")]
    assert run_called is False

    seen["stop_event"].set()
    await asyncio.wait_for(task, timeout=0.5)

    assert all(state != "ready" for state, _ in heartbeat.states)
    assert heartbeat.states[-1] == (
        "stopping",
        "shutdown_during_initialization",
    )


@pytest.mark.asyncio
async def test_run_worker_process_marks_ready_only_after_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = Container(bus=_FakeBus())
    heartbeat = _RecordingHeartbeat()
    worker = _BlockingWorker()
    initialized = asyncio.Event()
    seen: dict[str, asyncio.Event] = {}

    def fake_install(stop_event: asyncio.Event):
        seen["stop_event"] = stop_event
        return lambda: None

    async def initialize() -> None:
        initialized.set()

    async def noop() -> None:
        return None

    monkeypatch.setattr(runtime, "_install_shutdown_handlers", fake_install)
    monkeypatch.setattr(runtime, "close_redis", noop)
    monkeypatch.setattr(runtime, "dispose_engine", noop)

    task = asyncio.create_task(
        runtime.run_worker_process(
            "outbound",
            initialize=initialize,
            run=worker.run,
            stop=worker.stop,
            container=container,
            heartbeat=heartbeat,  # type: ignore[arg-type]
        )
    )
    await initialized.wait()
    await worker.started.wait()

    assert heartbeat.states[:2] == [("starting", ""), ("ready", "")]

    seen["stop_event"].set()
    await asyncio.wait_for(task, timeout=0.5)

    assert ("stopping", "shutdown_requested") in heartbeat.states
    assert heartbeat.states[-1][0] == "stopping"


@pytest.mark.asyncio
async def test_run_worker_process_marks_failed_initialization_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = Container(bus=_FakeBus())
    heartbeat = _RecordingHeartbeat()
    run_called = False

    async def initialize() -> None:
        raise ConnectionError("redis unavailable")

    async def run() -> None:
        nonlocal run_called
        run_called = True

    async def noop() -> None:
        return None

    monkeypatch.setattr(runtime, "_install_shutdown_handlers", lambda _event: lambda: None)
    monkeypatch.setattr(runtime, "close_redis", noop)
    monkeypatch.setattr(runtime, "dispose_engine", noop)

    with pytest.raises(ConnectionError, match="redis unavailable"):
        await runtime.run_worker_process(
            "outbound",
            initialize=initialize,
            run=run,
            stop=None,
            container=container,
            heartbeat=heartbeat,  # type: ignore[arg-type]
        )

    assert run_called is False
    assert all(state != "ready" for state, _ in heartbeat.states)
    assert heartbeat.states[-1] == (
        "degraded",
        "initialization_failed:ConnectionError",
    )


@pytest.mark.asyncio
async def test_run_worker_process_does_not_initialize_or_consume_when_unready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = Container(bus=_FakeBus())
    heartbeat = _RecordingHeartbeat()
    initialized = False
    run_called = False

    async def readiness() -> RoleReadiness:
        return RoleReadiness(
            role="inbound",
            checks={"redis": True, "db": False},
            required=("redis", "db"),
            errors=("db_unreachable",),
        )

    async def initialize() -> None:
        nonlocal initialized
        initialized = True

    async def run() -> None:
        nonlocal run_called
        run_called = True

    async def noop() -> None:
        return None

    monkeypatch.setattr(runtime, "_install_shutdown_handlers", lambda _event: lambda: None)
    monkeypatch.setattr(runtime, "close_redis", noop)
    monkeypatch.setattr(runtime, "dispose_engine", noop)

    with pytest.raises(runtime.WorkerReadinessLostError, match="db_unreachable"):
        await runtime.run_worker_process(
            "inbound",
            initialize=initialize,
            run=run,
            stop=None,
            container=container,
            heartbeat=heartbeat,  # type: ignore[arg-type]
            readiness_check=readiness,
        )

    assert initialized is False
    assert run_called is False
    assert all(state != "ready" for state, _ in heartbeat.states)
    assert heartbeat.states[-1] == (
        "degraded",
        "readiness_failed:WorkerReadinessLostError",
    )


@pytest.mark.asyncio
async def test_run_worker_process_stops_consumption_when_readiness_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _FakeBus()
    container = Container(bus=bus)
    heartbeat = _RecordingHeartbeat()
    worker = _BlockingWorker()
    checks = 0

    async def readiness() -> RoleReadiness:
        nonlocal checks
        checks += 1
        if checks <= 2:
            return RoleReadiness(
                role="outbound",
                checks={"redis": True, "db": True},
                required=("redis", "db"),
                errors=(),
            )
        return RoleReadiness(
            role="outbound",
            checks={"redis": True, "db": False},
            required=("redis", "db"),
            errors=("db_unreachable",),
        )

    async def noop() -> None:
        return None

    monkeypatch.setattr(runtime, "_install_shutdown_handlers", lambda _event: lambda: None)
    monkeypatch.setattr(runtime, "close_redis", noop)
    monkeypatch.setattr(runtime, "dispose_engine", noop)

    with pytest.raises(runtime.WorkerReadinessLostError, match="db_unreachable"):
        await runtime.run_worker_process(
            "outbound",
            run=worker.run,
            stop=worker.stop,
            container=container,
            heartbeat=heartbeat,  # type: ignore[arg-type]
            readiness_check=readiness,
            readiness_interval_seconds=0.01,
            shutdown_timeout_seconds=0.2,
        )

    assert worker.started.is_set()
    assert worker.stop_calls == 1
    assert bus.closed == 1
    assert ("degraded", "readiness_lost:WorkerReadinessLostError") in heartbeat.states
