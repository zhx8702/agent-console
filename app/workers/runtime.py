from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager, suppress
from threading import Thread
from typing import TYPE_CHECKING, Any, Protocol

from prometheus_client import start_http_server

from app.common.config import Settings
from app.common.logging import get_logger
from app.container import (
    Container,
    ContainerLike,
    OutboundContainer,
    SchedulerContainer,
    WxbotBridgeContainer,
)
from app.infra.db import dispose_engine
from app.infra.otel import setup_worker_tracing
from app.infra.redis_client import close_redis
from app.workers.heartbeat import WorkerHeartbeat, WorkerLifecycleState

if TYPE_CHECKING:
    import httpx

    from app.bus import MessageBus
    from app.plugin.registry import PluginRegistry

log = get_logger(__name__)

_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)
_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 15.0
_DEFAULT_READINESS_INTERVAL_SECONDS = 5.0


class _MetricsHTTPServer(Protocol):
    def shutdown(self) -> None: ...

    def server_close(self) -> None: ...


MetricsServer = tuple[_MetricsHTTPServer, Thread]


def _start_metrics_server(
    worker_name: str,
    *,
    host: str,
    port: int,
) -> MetricsServer | None:
    if port <= 0:
        return None
    server, thread = start_http_server(port, addr=host)
    log.info(
        "worker.metrics_started",
        worker=worker_name,
        host=host,
        port=port,
    )
    return server, thread


def _stop_metrics_server(
    worker_name: str,
    metrics_server: MetricsServer,
    *,
    timeout_seconds: float,
) -> None:
    server, thread = metrics_server
    try:
        server.shutdown()
    finally:
        try:
            server.server_close()
        finally:
            thread.join(timeout=max(0.0, timeout_seconds))
    if thread.is_alive():
        log.warning(
            "worker.metrics_shutdown_timeout",
            worker=worker_name,
            timeout_seconds=timeout_seconds,
        )
    else:
        log.info("worker.metrics_stopped", worker=worker_name)


class WorkerReadinessLostError(RuntimeError):
    """Raised when a worker must stop before consuming with stale dependencies."""

    def __init__(self, worker_name: str, detail: str) -> None:
        self.worker_name = worker_name
        self.detail = detail
        super().__init__(f"{worker_name} readiness lost: {detail}")


async def _assert_worker_ready(
    worker_name: str,
    readiness_check: Callable[[], Awaitable[object]],
) -> None:
    try:
        result = await readiness_check()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise WorkerReadinessLostError(
            worker_name,
            f"probe_failed:{exc.__class__.__name__}",
        ) from exc

    if isinstance(result, bool):
        ready = result
        detail = "dependency_unready"
    else:
        ready = bool(getattr(result, "ready", False))
        detail = str(getattr(result, "detail", "dependency_unready") or "dependency_unready")
    if not ready:
        raise WorkerReadinessLostError(worker_name, detail[:256])


async def _monitor_worker_readiness(
    worker_name: str,
    readiness_check: Callable[[], Awaitable[object]],
    *,
    interval_seconds: float,
) -> None:
    interval = max(0.1, float(interval_seconds))
    while True:
        await asyncio.sleep(interval)
        await _assert_worker_ready(worker_name, readiness_check)


async def _run_initializer(
    initialize: Callable[[], Awaitable[None]],
) -> None:
    await initialize()


@contextmanager
def worker_process_settings(role: str) -> Iterator[Settings]:
    """Temporarily bind a worker role without leaking it to the parent process."""

    import app.common.config as config_module

    previous_role = os.environ.get("APP_PROCESS_ROLE")
    os.environ["APP_PROCESS_ROLE"] = role
    get_settings = config_module.get_settings
    cache_clear = getattr(get_settings, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()
    try:
        yield get_settings()
    finally:
        if previous_role is None:
            os.environ.pop("APP_PROCESS_ROLE", None)
        else:
            os.environ["APP_PROCESS_ROLE"] = previous_role
        if callable(cache_clear):
            cache_clear()


def _request_shutdown(stop_event: asyncio.Event, sig: signal.Signals) -> None:
    if stop_event.is_set():
        return
    log.info("worker.shutdown_signal", signal=sig.name)
    stop_event.set()


def _install_shutdown_handlers(stop_event: asyncio.Event) -> Callable[[], None]:
    loop = asyncio.get_running_loop()
    removers: list[Callable[[], None]] = []

    for sig in _SHUTDOWN_SIGNALS:
        try:
            loop.add_signal_handler(sig, _request_shutdown, stop_event, sig)
        except (NotImplementedError, RuntimeError):
            previous = signal.getsignal(sig)

            def _handler(signum: int, frame: Any, *, previous_handler: Any = previous) -> None:
                _ = frame
                _request_shutdown(stop_event, signal.Signals(signum))
                if callable(previous_handler):
                    previous_handler(signum, frame)

            signal.signal(sig, _handler)

            def restore_handler(
                sig: signal.Signals = sig,
                previous_handler: Any = previous,
            ) -> None:
                signal.signal(sig, previous_handler)

            removers.append(restore_handler)
        else:

            def remove_loop_handler(sig: signal.Signals = sig) -> None:
                loop.remove_signal_handler(sig)

            removers.append(remove_loop_handler)

    def _cleanup() -> None:
        for remove in removers:
            with suppress(Exception):
                remove()

    return _cleanup


def _container_bus(container: ContainerLike) -> MessageBus | None:
    if isinstance(container, SchedulerContainer):
        return None
    return container.bus


def _container_plugin_registry(container: ContainerLike) -> PluginRegistry | None:
    if isinstance(container, (OutboundContainer, WxbotBridgeContainer)):
        return None
    return container.plugin_registry


def _container_http_client(container: ContainerLike) -> httpx.AsyncClient | None:
    if isinstance(container, OutboundContainer):
        return container.http_client
    if isinstance(container, Container):
        return container.http_client
    return None


async def _close_bus(container: ContainerLike) -> bool:
    bus = _container_bus(container)
    if bus is None:
        return False
    try:
        await bus.close()
    except Exception:
        log.exception("worker.bus_close_failed")
        return False
    return True


async def _cleanup_worker_resources(
    container: ContainerLike,
    *,
    close_bus: bool = True,
) -> None:
    plugin_registry = _container_plugin_registry(container)
    if plugin_registry is not None:
        with suppress(Exception):
            await plugin_registry.shutdown_all()

    if close_bus:
        await _close_bus(container)

    http_client = _container_http_client(container)
    if http_client is not None:
        with suppress(Exception):
            await http_client.aclose()

    await close_redis()
    await dispose_engine()


async def run_worker_process(
    worker_name: str,
    *,
    initialize: Callable[[], Awaitable[None]] | None = None,
    run: Callable[[], Coroutine[Any, Any, None]],
    stop: Callable[[], Awaitable[None]] | None,
    container: ContainerLike,
    shutdown_timeout_seconds: float = _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    before_cleanup: Callable[[], Awaitable[None]] | None = None,
    heartbeat: WorkerHeartbeat | None = None,
    readiness_check: Callable[[], Awaitable[object]] | None = None,
    readiness_interval_seconds: float = _DEFAULT_READINESS_INTERVAL_SECONDS,
    metrics_host: str = "127.0.0.1",
    metrics_port: int = 0,
) -> None:
    stop_event = asyncio.Event()
    cleanup_handlers = _install_shutdown_handlers(stop_event)
    stop_task = asyncio.create_task(stop_event.wait(), name=f"{worker_name}-shutdown")
    worker_task: asyncio.Task[None] | None = None
    initialize_task: asyncio.Task[None] | None = None
    readiness_task: asyncio.Task[None] | None = None
    bus_closed = False
    worker_error: BaseException | None = None
    cancelled_for_shutdown = False
    final_heartbeat_state: WorkerLifecycleState = "stopping"
    final_heartbeat_detail = ""
    metrics_server: MetricsServer | None = None

    try:
        setup_worker_tracing()
        metrics_server = _start_metrics_server(
            worker_name,
            host=metrics_host,
            port=metrics_port,
        )
        if heartbeat is not None:
            await heartbeat.start()

        if readiness_check is not None:
            try:
                await _assert_worker_ready(worker_name, readiness_check)
            except BaseException as exc:
                final_heartbeat_state = "degraded"
                final_heartbeat_detail = f"readiness_failed:{exc.__class__.__name__}"
                worker_error = exc
                raise

        if initialize is not None:
            initialize_task = asyncio.create_task(
                _run_initializer(initialize),
                name=f"{worker_name}-initialize",
            )
            done, _ = await asyncio.wait(
                {initialize_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done and stop_event.is_set() and not initialize_task.done():
                final_heartbeat_detail = "shutdown_during_initialization"
                initialize_task.cancel()
                with suppress(asyncio.CancelledError):
                    await initialize_task
                return
            try:
                await initialize_task
            except BaseException as exc:
                final_heartbeat_state = "degraded"
                final_heartbeat_detail = f"initialization_failed:{exc.__class__.__name__}"
                worker_error = exc
                raise

        # Initializers may perform network I/O for several seconds. Recheck at
        # the exact consume boundary so a dependency lost during startup never
        # results in a ready heartbeat or one consumed message.
        if readiness_check is not None:
            try:
                await _assert_worker_ready(worker_name, readiness_check)
            except BaseException as exc:
                final_heartbeat_state = "degraded"
                final_heartbeat_detail = f"readiness_failed:{exc.__class__.__name__}"
                worker_error = exc
                raise

        if stop_event.is_set():
            final_heartbeat_detail = "shutdown_before_ready"
            return
        if heartbeat is not None:
            await heartbeat.mark_ready()
        worker_task = asyncio.create_task(run(), name=f"{worker_name}-worker")
        if readiness_check is not None:
            readiness_task = asyncio.create_task(
                _monitor_worker_readiness(
                    worker_name,
                    readiness_check,
                    interval_seconds=readiness_interval_seconds,
                ),
                name=f"{worker_name}-readiness",
            )
        wait_for = {worker_task, stop_task}
        if readiness_task is not None:
            wait_for.add(readiness_task)
        done, _ = await asyncio.wait(
            wait_for,
            return_when=asyncio.FIRST_COMPLETED,
        )

        active_readiness_task = readiness_task
        readiness_lost = active_readiness_task is not None and active_readiness_task in done
        if readiness_lost:
            assert active_readiness_task is not None
            try:
                await active_readiness_task
            except BaseException as exc:
                worker_error = exc
                final_heartbeat_state = "degraded"
                final_heartbeat_detail = f"readiness_lost:{exc.__class__.__name__}"
            else:
                worker_error = WorkerReadinessLostError(
                    worker_name,
                    "readiness_monitor_exited",
                )
                final_heartbeat_state = "degraded"
                final_heartbeat_detail = "readiness_monitor_exited"
            if heartbeat is not None:
                await heartbeat.set_state(
                    "degraded",
                    detail=final_heartbeat_detail,
                )
            log.error(
                "worker.readiness_lost",
                worker=worker_name,
                detail=final_heartbeat_detail,
            )
            if stop is not None:
                with suppress(Exception):
                    await stop()
            bus_closed = await _close_bus(container)
            try:
                await asyncio.wait_for(worker_task, timeout=shutdown_timeout_seconds)
            except TimeoutError:
                worker_task.cancel()
            except BaseException:
                # The worker's own error is collected below. Readiness loss
                # remains the primary process failure for this branch.
                pass

        if (
            not readiness_lost
            and stop_task in done
            and stop_event.is_set()
            and not worker_task.done()
        ):
            if heartbeat is not None:
                await heartbeat.set_state(
                    "stopping",
                    detail="shutdown_requested",
                )
            log.info(
                "worker.shutdown_requested",
                worker=worker_name,
                timeout_seconds=shutdown_timeout_seconds,
            )
            if stop is not None:
                with suppress(Exception):
                    await stop()
            bus_closed = await _close_bus(container)
            try:
                await asyncio.wait_for(worker_task, timeout=shutdown_timeout_seconds)
            except TimeoutError:
                cancelled_for_shutdown = True
                log.warning(
                    "worker.shutdown_timeout",
                    worker=worker_name,
                    timeout_seconds=shutdown_timeout_seconds,
                )
                worker_task.cancel()

        if not stop_task.done():
            stop_task.cancel()
        with suppress(asyncio.CancelledError):
            await stop_task

        try:
            await worker_task
        except asyncio.CancelledError:
            if not cancelled_for_shutdown and not readiness_lost:
                final_heartbeat_state = "degraded"
                final_heartbeat_detail = "worker_cancelled"
                raise
        except BaseException as exc:
            if worker_error is None:
                worker_error = exc
                final_heartbeat_state = "degraded"
                final_heartbeat_detail = f"worker_failed:{exc.__class__.__name__}"
        else:
            if not stop_event.is_set() and not readiness_lost:
                final_heartbeat_state = "degraded"
                final_heartbeat_detail = "worker_exited"
    finally:
        cleanup_handlers()
        if not stop_task.done():
            stop_task.cancel()
        with suppress(asyncio.CancelledError):
            await stop_task
        if initialize_task is not None and not initialize_task.done():
            initialize_task.cancel()
            with suppress(asyncio.CancelledError):
                await initialize_task
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task
        if readiness_task is not None and not readiness_task.done():
            readiness_task.cancel()
            with suppress(asyncio.CancelledError):
                await readiness_task
        if heartbeat is not None:
            with suppress(Exception):
                await heartbeat.stop(
                    final_state=final_heartbeat_state,
                    detail=final_heartbeat_detail,
                )
        if before_cleanup is not None:
            with suppress(Exception):
                await before_cleanup()
        try:
            await _cleanup_worker_resources(container, close_bus=not bus_closed)
        finally:
            if metrics_server is not None:
                try:
                    _stop_metrics_server(
                        worker_name,
                        metrics_server,
                        timeout_seconds=shutdown_timeout_seconds,
                    )
                except Exception:
                    log.exception(
                        "worker.metrics_stop_failed",
                        worker=worker_name,
                    )

    if worker_error is not None:
        raise worker_error


__all__ = [
    "WorkerReadinessLostError",
    "run_worker_process",
    "worker_process_settings",
]
