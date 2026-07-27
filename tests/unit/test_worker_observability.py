from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.common.config import Settings
from app.container import Container
from app.infra.otel import build_trace_resource
from app.workers import runtime

ROOT = Path(__file__).resolve().parents[2]
WORKER_SERVICES = (
    "inbound-worker",
    "outbound-worker",
    "scheduler",
    "wxbot-bridge-worker",
)


def _yaml(name: str) -> dict[str, Any]:
    payload = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_worker_metrics_are_disabled_by_default_and_trace_resource_is_identifiable() -> None:
    settings = Settings(
        app_service_name="agent-console-test",
        app_process_role="scheduler",
        worker_instance_id="scheduler-a",
    )

    assert settings.worker_metrics_host == "127.0.0.1"
    assert settings.worker_metrics_port == 0
    attributes = build_trace_resource(settings).attributes
    assert attributes["service.name"] == "agent-console-test"
    assert attributes["service.instance.id"] == "scheduler-a"
    assert attributes["process.role"] == "scheduler"
    assert attributes["deployment.environment"] == "test"


@pytest.mark.asyncio
async def test_worker_metrics_server_is_closed_when_worker_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class FakeServer:
        def shutdown(self) -> None:
            calls.append("shutdown")

        def server_close(self) -> None:
            calls.append("server_close")

    class FakeThread:
        def join(self, timeout: float | None = None) -> None:
            calls.append(("join", timeout))

        def is_alive(self) -> bool:
            return False

    def fake_start(port: int, addr: str) -> tuple[FakeServer, FakeThread]:
        calls.append(("start", addr, port))
        return FakeServer(), FakeThread()

    async def noop() -> None:
        return None

    async def fail() -> None:
        raise RuntimeError("worker failed")

    monkeypatch.setattr(runtime, "setup_worker_tracing", lambda: None)
    monkeypatch.setattr(runtime, "start_http_server", fake_start)
    monkeypatch.setattr(runtime, "_install_shutdown_handlers", lambda _event: lambda: None)
    monkeypatch.setattr(runtime, "close_redis", noop)
    monkeypatch.setattr(runtime, "dispose_engine", noop)

    with pytest.raises(RuntimeError, match="worker failed"):
        await runtime.run_worker_process(
            "scheduler",
            run=fail,
            stop=None,
            container=Container(),
            shutdown_timeout_seconds=2.0,
            metrics_host="127.0.0.1",
            metrics_port=9100,
        )

    assert calls == [
        ("start", "127.0.0.1", 9100),
        "shutdown",
        "server_close",
        ("join", 2.0),
    ]


def test_compose_and_collector_observability_contract() -> None:
    compose = _yaml("docker-compose.yml")
    production = _yaml("docker-compose.production.yml")
    collector = _yaml("config/otel-collector.yaml")

    for anchor_name in ("x-app-env", "x-wxbot-adapter-env"):
        environment = compose[anchor_name]
        assert environment["OTEL_EXPORTER_OTLP_ENDPOINT"] == (
            "${COMPOSE_OTEL_EXPORTER_OTLP_ENDPOINT:-http://otel-collector:4317}"
        )
        assert environment["OTEL_EXPORTER_OTLP_INSECURE"] == (
            "${COMPOSE_OTEL_EXPORTER_OTLP_INSECURE:-true}"
        )

    services = compose["services"]
    production_services = production["services"]
    for service_name in WORKER_SERVICES:
        environment = services[service_name]["environment"]
        assert environment["WORKER_METRICS_HOST"] == "0.0.0.0"
        assert environment["WORKER_METRICS_PORT"] == "9100"
        assert production_services[service_name]["expose"] == ["9100"]

    prometheus = collector["receivers"]["prometheus"]["config"]
    targets = {
        target
        for job in prometheus["scrape_configs"]
        for static_config in job["static_configs"]
        for target in static_config["targets"]
    }
    assert targets == {
        "api:8000",
        "inbound-worker:9100",
        "outbound-worker:9100",
        "scheduler:9100",
        "wxbot-bridge-worker:9100",
    }
    assert collector["service"]["pipelines"]["metrics"]["receivers"] == [
        "otlp",
        "prometheus",
    ]


def test_worker_entrypoints_and_production_runbook_keep_observability_contract() -> None:
    for name in (
        "inbound_worker.py",
        "outbound_worker.py",
        "scheduler_worker.py",
        "wxbot_bridge_worker.py",
    ):
        source = (ROOT / "app" / "workers" / name).read_text(encoding="utf-8")
        assert "metrics_host=settings.worker_metrics_host" in source
        assert "metrics_port=settings.worker_metrics_port" in source

    runbook = (ROOT / "docs" / "production-deployment.md").read_text(encoding="utf-8")
    assert "compatibility level `4`" in runbook
    assert "periodically while running" in runbook
    assert "private port `9100`" in runbook
    assert "`process.role`" in runbook
