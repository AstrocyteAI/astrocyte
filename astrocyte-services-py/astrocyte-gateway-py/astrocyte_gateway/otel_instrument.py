"""OpenTelemetry FastAPI instrumentation (optional ``[otel]`` extra)."""

from __future__ import annotations

import os
from typing import Any


def instrument_app(app: Any) -> None:
    """Wire OTLP HTTP exporter (env: ``OTEL_EXPORTER_OTLP_ENDPOINT``) + FastAPI spans."""
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    service_name = os.environ.get("OTEL_SERVICE_NAME", "astrocyte-gateway-py")
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor().instrument_app(app)  # type: ignore[no-untyped-call]
