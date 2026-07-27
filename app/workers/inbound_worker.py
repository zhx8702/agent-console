"""
Inbound worker: consumes the inbound stream produced by the ingress gateway
and runs each message through the Dialog Orchestrator.

The orchestrator is responsible for idempotency *of its own side-effects*
(session, turns). This worker only needs to translate a BusMessage back to
an InboundEvent and dispatch. Retries + DLQ are handled by the bus layer.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime

from prometheus_client import Counter, Histogram

from app.bus.base import BusMessage, MessageBus, PermanentMessageError
from app.common.config import Settings
from app.common.context import clear_context, set_session_id, set_tenant_id, set_trace_id
from app.common.logging import configure_logging, get_logger
from app.common.types import InboundEvent
from app.container import InboundContainer, set_container
from app.infra.redis_client import get_redis
from app.orchestrator.engine import DialogOrchestrator
from app.orchestrator.outcome import (
    PermanentProcessingError,
    ProcessingOutcome,
    ProcessingStatus,
    RetryableProcessingError,
    normalize_processing_outcome,
)
from app.orchestrator.owner_gate import (
    evaluate_owner_execution,
    owner_gate_failure_is_retryable,
)
from app.orchestrator.pipeline import PipelineContext
from app.reliability import MessageEffectIntentRelay
from app.workers.heartbeat import WorkerHeartbeat
from app.workers.readiness import probe_role_dependencies
from app.workers.runtime import run_worker_process, worker_process_settings

log = get_logger(__name__)

INBOUND_PROCESSING_OUTCOMES = Counter(
    "cs_inbound_processing_outcomes_total",
    "Final inbound message dispositions observed by the worker",
    ["status"],
)
INBOUND_END_TO_END_LATENCY = Histogram(
    "cs_inbound_end_to_end_latency_seconds",
    "Time from ingress receipt to the worker's final processing disposition",
    ["status", "channel"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 8, 15, 30, 60, 120, 300),
)


class InboundWorker:
    def __init__(
        self,
        bus: MessageBus,
        orchestrator: DialogOrchestrator,
        settings: Settings,
        *,
        consumer_name: str | None = None,
        effect_intent_relay: MessageEffectIntentRelay | None = None,
    ) -> None:
        self._bus = bus
        self._orchestrator = orchestrator
        self._settings = settings
        self._consumer_name = consumer_name or settings.resolved_inbound_worker_consumer_name
        if effect_intent_relay is None:
            message_store = getattr(orchestrator, "message_store", None)
            effect_registry = getattr(
                orchestrator,
                "flow_effect_handler_registry",
                None,
            )
            if message_store is not None and effect_registry is not None:
                plugin_registry = getattr(orchestrator, "plugin_registry", None)
                owner_gate = getattr(plugin_registry, "execution_allowed", None)
                effect_intent_relay = MessageEffectIntentRelay(
                    message_store,
                    effect_registry,
                    worker_id=self._consumer_name,
                    poll_interval_seconds=settings.effect_intent_relay_poll_interval_seconds,
                    batch_size=settings.effect_intent_relay_batch_size,
                    lease_seconds=settings.effect_intent_relay_lease_seconds,
                    handler_timeout_seconds=(
                        settings.effect_intent_relay_handler_timeout_seconds
                    ),
                    max_attempts=settings.effect_intent_relay_max_attempts,
                    owner_gate=owner_gate if callable(owner_gate) else None,
                )
        self._effect_intent_relay = effect_intent_relay
        self._stop = asyncio.Event()
        self._initialized = False

    def _loaded_plugin(self, name: str) -> object | None:
        registry = getattr(self._orchestrator, "plugin_registry", None)
        if registry is None:
            registry = getattr(self._orchestrator, "_plugin_registry", None)
        if registry is None:
            return None
        plugins = getattr(registry, "loaded_plugins", {})
        return plugins.get(name) if isinstance(plugins, dict) else None

    async def _scope_owner_allowed(
        self,
        owner: str,
        event: InboundEvent,
    ) -> bool:
        registry = getattr(self._orchestrator, "plugin_registry", None)
        if registry is None:
            registry = getattr(self._orchestrator, "_plugin_registry", None)
        gate = getattr(registry, "execution_allowed", None)
        if not callable(gate):
            log.error(
                "inbound_worker.plugin_owner_gate_missing",
                plugin=owner,
            )
            return False

        ctx = PipelineContext(event=event, trace_id=event.trace_id)
        decision = await evaluate_owner_execution(gate, owner, ctx)
        if decision.allowed:
            return True
        if owner_gate_failure_is_retryable(decision.reason):
            raise RetryableProcessingError(
                decision.reason,
                error_type="InboundPluginOwnerGateUnavailable",
            )
        log.info(
            "inbound_worker.plugin_owner_skipped",
            plugin=owner,
            tenant_id=event.tenant_id,
            session_id=event.session_id,
            reason=decision.reason,
        )
        return False

    async def initialize(self) -> None:
        if self._initialized:
            return
        stream = self._settings.bus_inbound_stream
        group = self._settings.bus_consumer_group

        await self._bus.ensure_group(stream, group)
        log.info(
            "inbound_worker.starting",
            stream=stream,
            group=group,
            consumer=self._consumer_name,
        )
        if self._effect_intent_relay is not None:
            await self._effect_intent_relay.prepare_worker()
        self._initialized = True

    async def run(self) -> None:
        await self.initialize()
        stream = self._settings.bus_inbound_stream
        group = self._settings.bus_consumer_group

        drain_tasks: list[asyncio.Task[None]] = []
        if self._effect_intent_relay is not None:
            drain_tasks.append(
                asyncio.create_task(
                    self._effect_intent_relay.run(),
                    name="inbound-effect-intent-relay",
                )
            )
        try:
            async for _ in self._bus.consume(
                stream=stream,
                group=group,
                consumer=self._consumer_name,
                handler=self._handle,
                batch_size=self._settings.bus_consume_batch_size,
                block_ms=self._settings.bus_consume_block_ms,
            ):
                if self._stop.is_set():
                    break
        finally:
            self._stop.set()
            for drain_task in drain_tasks:
                if not drain_task.done():
                    drain_task.cancel()
            for drain_task in drain_tasks:
                with suppress(asyncio.CancelledError):
                    await drain_task

        log.info("inbound_worker.stopped")

    async def stop(self) -> None:
        self._stop.set()
        if self._effect_intent_relay is not None:
            await self._effect_intent_relay.stop()

    async def _handle(self, msg: BusMessage) -> None:
        try:
            event = InboundEvent.model_validate(msg.payload)
        except Exception as exc:
            log.exception("inbound_worker.invalid_payload", error=str(exc))
            # Re-raising lets the bus move it to DLQ after max attempts.
            raise

        set_trace_id(event.trace_id)
        set_tenant_id(event.tenant_id)
        set_session_id(event.session_id)

        log.info(
            "inbound_worker.handle",
            message_id=event.message_id,
            session_id=event.session_id,
            channel=event.channel.value if hasattr(event.channel, "value") else event.channel,
        )
        try:
            wxbot_plugin = self._loaded_plugin("wxbot")
            record_group_observation = getattr(
                wxbot_plugin,
                "record_group_observation",
                None,
            )
            if callable(record_group_observation) and await self._scope_owner_allowed(
                "wxbot",
                event,
            ):
                await record_group_observation(event)
            try:
                raw_outcome = await self._orchestrator.handle(event)
            except PermanentProcessingError as exc:
                outcome = ProcessingOutcome.permanent_failure(
                    reason=exc.reason,
                    error_type=exc.error_type,
                )
            except RetryableProcessingError:
                INBOUND_PROCESSING_OUTCOMES.labels(
                    status=ProcessingStatus.RETRYABLE_FAILURE.value
                ).inc()
                _observe_end_to_end(event, ProcessingStatus.RETRYABLE_FAILURE)
                raise
            except Exception:
                # Legacy orchestrators may still raise directly. Unknown
                # exceptions are retryable and must reach the bus unchanged.
                INBOUND_PROCESSING_OUTCOMES.labels(
                    status=ProcessingStatus.RETRYABLE_FAILURE.value
                ).inc()
                _observe_end_to_end(event, ProcessingStatus.RETRYABLE_FAILURE)
                raise
            else:
                outcome = normalize_processing_outcome(raw_outcome)

            if outcome.status == ProcessingStatus.RETRYABLE_FAILURE:
                INBOUND_PROCESSING_OUTCOMES.labels(status=outcome.status.value).inc()
                _observe_end_to_end(event, outcome.status)
                raise RetryableProcessingError.from_outcome(outcome)

            if outcome.status == ProcessingStatus.PERMANENT_FAILURE:
                reason = (
                    f"permanent:{outcome.reason}"
                    if outcome.reason
                    else "permanent:processing_failure"
                )
                INBOUND_PROCESSING_OUTCOMES.labels(status=outcome.status.value).inc()
                _observe_end_to_end(event, outcome.status)
                log.error(
                    "inbound_worker.permanent_failure",
                    message_id=event.message_id,
                    reason=outcome.reason,
                    error_type=outcome.error_type,
                    dlq_persisted=False,
                    disposition="atomic_dlq_requested",
                )
                # RedisStreamBus recognizes this disposition and performs
                # XADD(DLQ)+XACK(source) in one Lua script. If that script
                # fails, the source remains pending for safe redelivery.
                raise PermanentMessageError(reason)

            if not outcome.ackable:
                # Defensive future-proofing if a new status is introduced but
                # worker handling is not updated in the same release.
                _observe_end_to_end(event, ProcessingStatus.RETRYABLE_FAILURE)
                raise RetryableProcessingError(
                    f"unsupported_processing_status:{outcome.status}"
                )

            INBOUND_PROCESSING_OUTCOMES.labels(status=outcome.status.value).inc()
            _observe_end_to_end(event, outcome.status)
            log.info(
                "inbound_worker.outcome",
                message_id=event.message_id,
                status=outcome.status.value,
                route=outcome.route_label,
                reason=outcome.reason,
            )
        finally:
            clear_context()


def _event_age_seconds(event: InboundEvent, *, now: datetime | None = None) -> float:
    received_at = event.received_at
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return max(0.0, (current - received_at).total_seconds())


def _observe_end_to_end(event: InboundEvent, status: ProcessingStatus) -> None:
    channel = event.channel.value if hasattr(event.channel, "value") else str(event.channel)
    INBOUND_END_TO_END_LATENCY.labels(
        status=status.value,
        channel=channel,
    ).observe(_event_age_seconds(event))


async def run_inbound_worker() -> None:
    with worker_process_settings("inbound") as settings:
        from app.main import build_container

        configure_logging()
        container = await build_container(settings)
        if not isinstance(container, InboundContainer):
            raise RuntimeError("inbound worker requires an InboundContainer")
        set_container(container)
        worker = InboundWorker(container.bus, container.orchestrator, settings)
        redis = get_redis()
        await run_worker_process(
            "inbound",
            initialize=worker.initialize,
            run=worker.run,
            stop=worker.stop,
            container=container,
            shutdown_timeout_seconds=settings.worker_shutdown_timeout_seconds,
            heartbeat=WorkerHeartbeat.from_settings(redis, settings),
            readiness_check=lambda: probe_role_dependencies(
                "inbound",
                settings,
                redis=redis,
            ),
            readiness_interval_seconds=settings.worker_heartbeat_interval_seconds,
            metrics_host=settings.worker_metrics_host,
            metrics_port=settings.worker_metrics_port,
        )


def main() -> None:
    asyncio.run(run_inbound_worker())


if __name__ == "__main__":
    main()
