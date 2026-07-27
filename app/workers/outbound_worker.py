"""
Outbound worker: thin wrapper around OutboundDispatcher.run_worker that
provides lifecycle symmetry with the inbound worker.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress

from app.common.logging import configure_logging, get_logger
from app.container import OutboundContainer, set_container
from app.egress.dispatcher import OutboundDispatcher
from app.infra.redis_client import get_redis
from app.reliability import MessageOutboxRelay
from app.workers.heartbeat import WorkerHeartbeat
from app.workers.readiness import probe_role_dependencies
from app.workers.runtime import run_worker_process, worker_process_settings

log = get_logger(__name__)


class OutboundWorker:
    def __init__(
        self,
        dispatcher: OutboundDispatcher,
        outbox_relay: MessageOutboxRelay | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._outbox_relay = outbox_relay
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self._dispatcher.prepare_worker()
        if self._outbox_relay is not None:
            await self._outbox_relay.prepare_worker()
        self._initialized = True

    async def run(self) -> None:
        await self.initialize()
        log.info("outbound_worker.starting")
        if self._outbox_relay is None:
            await self._dispatcher.run_worker()
            log.info("outbound_worker.stopped")
            return

        dispatcher_task = asyncio.create_task(
            self._dispatcher.run_worker(),
            name="outbound-dispatcher",
        )
        relay_task = asyncio.create_task(
            self._outbox_relay.run(),
            name="outbox-relay",
        )
        try:
            await asyncio.gather(dispatcher_task, relay_task)
        finally:
            for task in (dispatcher_task, relay_task):
                if not task.done():
                    task.cancel()
            for task in (dispatcher_task, relay_task):
                with suppress(asyncio.CancelledError):
                    await task
        log.info("outbound_worker.stopped")

    async def stop(self) -> None:
        if self._outbox_relay is not None:
            await self._outbox_relay.stop()


async def run_outbound_worker() -> None:
    with worker_process_settings("outbound") as settings:
        from app.main import build_container

        configure_logging()
        container = await build_container(settings)
        if not isinstance(container, OutboundContainer):
            raise RuntimeError("outbound worker requires an OutboundContainer")
        set_container(container)
        redis = get_redis()
        worker = OutboundWorker(
            container.dispatcher,
            container.outbox_relay,
        )
        await run_worker_process(
            "outbound",
            initialize=worker.initialize,
            run=worker.run,
            stop=worker.stop,
            container=container,
            shutdown_timeout_seconds=settings.worker_shutdown_timeout_seconds,
            heartbeat=WorkerHeartbeat.from_settings(redis, settings),
            readiness_check=lambda: probe_role_dependencies(
                "outbound",
                settings,
                redis=redis,
            ),
            readiness_interval_seconds=settings.worker_heartbeat_interval_seconds,
            metrics_host=settings.worker_metrics_host,
            metrics_port=settings.worker_metrics_port,
        )


def main() -> None:
    asyncio.run(run_outbound_worker())


if __name__ == "__main__":
    main()
