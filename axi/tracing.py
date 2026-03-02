"""OpenTelemetry tracing init — configures export to Jaeger via OTLP/gRPC."""

from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

log = logging.getLogger(__name__)

_provider: TracerProvider | None = None


def init_tracing(service_name: str) -> None:
    """Set up OTel TracerProvider with OTLP/gRPC exporter.

    Reads OTEL_ENDPOINT env var (default ``http://localhost:4317``).
    Gracefully degrades if Jaeger isn't running — spans are simply dropped.
    """
    global _provider

    endpoint = os.environ.get("OTEL_ENDPOINT", "http://localhost:4317")

    resource = Resource.create({"service.name": service_name})
    _provider = TracerProvider(resource=resource)

    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    _provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(_provider)
    log.info("OpenTelemetry tracing initialized (endpoint=%s, service=%s)", endpoint, service_name)


def shutdown_tracing() -> None:
    """Flush pending spans and shut down the provider."""
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None
        log.info("OpenTelemetry tracing shut down")
