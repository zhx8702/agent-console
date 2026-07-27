from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import (
    DEPLOYMENT_ENVIRONMENT,
    SERVICE_INSTANCE_ID,
    SERVICE_NAME,
    Resource,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from app.common.config import Settings, get_settings

_initialized = False


def build_trace_resource(settings: Settings) -> Resource:
    """Build process-identifying resource attributes shared by every span."""

    return Resource.create(
        {
            SERVICE_NAME: settings.app_service_name,
            SERVICE_INSTANCE_ID: settings.resolved_worker_instance_id,
            DEPLOYMENT_ENVIRONMENT: settings.app_env,
            "process.role": settings.app_process_role,
        }
    )


def setup_tracing(settings: Settings | None = None) -> trace.Tracer:
    global _initialized
    s = settings or get_settings()
    tracer = trace.get_tracer(s.app_service_name)
    if _initialized:
        return tracer

    sampler = ParentBased(TraceIdRatioBased(s.otel_traces_sampler_arg))
    provider = TracerProvider(resource=build_trace_resource(s), sampler=sampler)

    if s.otel_exporter_otlp_endpoint:
        exporter = OTLPSpanExporter(
            endpoint=s.otel_exporter_otlp_endpoint,
            insecure=s.otel_exporter_otlp_insecure,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
    elif s.app_env not in ("test", "ci"):
        # Dev: print spans to stdout for easy inspection. Skipped in tests
        # because pytest closes stdout and the background exporter thread
        # logs ValueError every time it tries to flush.
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _initialized = True
    return trace.get_tracer(s.app_service_name)


def setup_worker_tracing(settings: Settings | None = None) -> trace.Tracer:
    """Configure export plus idempotent client tracing for worker processes."""

    tracer = setup_tracing(settings)
    httpx_instrumentor = HTTPXClientInstrumentor()
    if not httpx_instrumentor.is_instrumented_by_opentelemetry:
        httpx_instrumentor.instrument()
    redis_instrumentor = RedisInstrumentor()
    if not redis_instrumentor.is_instrumented_by_opentelemetry:
        redis_instrumentor.instrument()
    return tracer


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(get_settings().app_service_name)


__all__ = [
    "build_trace_resource",
    "get_tracer",
    "setup_tracing",
    "setup_worker_tracing",
]
