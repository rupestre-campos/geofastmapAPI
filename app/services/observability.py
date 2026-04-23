"""OpenTelemetry bootstrap for API traces (Tempo via OTEL Collector)."""

from __future__ import annotations

import logging

from app.core.config import Settings

logger = logging.getLogger(__name__)

_initialized = False


def init_observability(settings: Settings) -> None:
    global _initialized
    if _initialized or not settings.observability_tracing_enabled:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
    except Exception as e:
        logger.warning("observability tracing disabled: OpenTelemetry imports failed: %s", e)
        return

    sample_ratio = max(0.0, min(1.0, float(settings.observability_trace_sample_ratio)))
    provider = TracerProvider(
        sampler=TraceIdRatioBased(sample_ratio),
        resource=Resource.create({"service.name": settings.observability_service_name}),
    )
    exporter = OTLPSpanExporter(endpoint=settings.observability_otlp_endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    HTTPXClientInstrumentor().instrument()
    _initialized = True


def instrument_fastapi_app(app) -> None:
    if not _initialized:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception as e:
        logger.warning("failed to instrument FastAPI app for tracing: %s", e)
