"""OpenTelemetry tracing init — configures export to Jaeger via OTLP/gRPC.

Provides init/shutdown lifecycle and the ``traced`` decorator for
auto-instrumenting async functions with spans and error recording.
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Callable
from typing import Any, TypeVar

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

log = logging.getLogger(__name__)

_provider: TracerProvider | None = None

F = TypeVar("F", bound=Callable[..., Any])


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


def traced(
    span_name: str | None = None,
    *,
    tracer_name: str = "axi",
    attributes: dict[str, Any] | None = None,
) -> Callable[[F], F]:
    """Decorator that wraps an async function in an OTel span.

    The span is named ``span_name`` (defaults to the function name).
    Exceptions are recorded on the span and re-raised.

    Usage::

        @traced("spawn_agent", attributes={"agent.type": "claude_code"})
        async def spawn_agent(name: str, ...) -> None:
            ...
    """

    def decorator(fn: F) -> F:
        name = span_name or fn.__qualname__

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = trace.get_tracer(tracer_name)
            with tracer.start_as_current_span(name, attributes=attributes or {}) as span:
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    span.set_status(trace.StatusCode.ERROR, str(exc))
                    span.record_exception(exc)
                    raise

        return wrapper  # type: ignore[return-value]

    return decorator
