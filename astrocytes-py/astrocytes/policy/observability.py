"""Observability policies — OTel spans, structured logging, metrics.

OTel and Prometheus are optional dependencies. Gracefully degrade to no-op.
See docs/_design/policy-layer.md section 5.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Generator

logger = logging.getLogger("astrocytes")

# ---------------------------------------------------------------------------
# OTel span manager (optional dependency)
# ---------------------------------------------------------------------------

try:
    from opentelemetry import trace

    _tracer = trace.get_tracer("astrocytes")
    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False
    _tracer = None


@contextmanager
def span(name: str, attributes: dict[str, str | int | float | bool] | None = None) -> Generator[Any, None, None]:
    """Create an OTel span. No-op if opentelemetry is not installed.

    Usage:
        with span("astrocytes.recall", {"bank_id": "user-123"}) as s:
            result = await provider.recall(request)
            s.set_attribute("result_count", len(result.hits))
    """
    if _HAS_OTEL and _tracer is not None:
        with _tracer.start_as_current_span(name, attributes=attributes or {}) as s:
            yield s
    else:
        yield _NoOpSpan()


class _NoOpSpan:
    """No-op span when OTel is not installed."""

    def set_attribute(self, key: str, value: str | int | float | bool) -> None:
        pass

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        pass

    def set_status(self, status: Any, description: str | None = None) -> None:
        pass


# ---------------------------------------------------------------------------
# Structured logger
# ---------------------------------------------------------------------------


@dataclass
class LogEntry:
    event: str
    timestamp: str
    bank_id: str | None = None
    provider: str | None = None
    operation: str | None = None
    trace_id: str | None = None
    data: dict[str, str | int | float | bool | None] | None = None


class StructuredLogger:
    """JSON structured logging for policy events.

    Sync — wraps Python logging module.
    """

    def __init__(self, level: str = "info") -> None:
        self._level = getattr(logging, level.upper(), logging.INFO)

    def log(
        self,
        event: str,
        bank_id: str | None = None,
        provider: str | None = None,
        operation: str | None = None,
        data: dict[str, str | int | float | bool | None] | None = None,
        level: int | None = None,
    ) -> None:
        """Emit a structured log entry."""
        entry = LogEntry(
            event=event,
            timestamp=datetime.now(timezone.utc).isoformat(),
            bank_id=bank_id,
            provider=provider,
            operation=operation,
            data=data,
        )
        log_level = level or self._level
        # Remove None values for cleaner output
        entry_dict = {k: v for k, v in asdict(entry).items() if v is not None}
        logger.log(log_level, json.dumps(entry_dict))


# ---------------------------------------------------------------------------
# Metrics collector (optional Prometheus)
# ---------------------------------------------------------------------------

try:
    from prometheus_client import Counter, Gauge, Histogram

    _HAS_PROMETHEUS = True
except ImportError:
    _HAS_PROMETHEUS = False


class MetricsCollector:
    """Prometheus metrics collection. No-op if prometheus_client is not installed."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and _HAS_PROMETHEUS
        self._counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}
        self._gauges: dict[str, Any] = {}

    def _get_counter(self, name: str, description: str, labels: list[str]) -> Any:
        if not self.enabled:
            return None
        if name not in self._counters:
            self._counters[name] = Counter(name, description, labels)
        return self._counters[name]

    def _get_histogram(self, name: str, description: str, labels: list[str]) -> Any:
        if not self.enabled:
            return None
        if name not in self._histograms:
            self._histograms[name] = Histogram(name, description, labels)
        return self._histograms[name]

    def _get_gauge(self, name: str, description: str, labels: list[str]) -> Any:
        if not self.enabled:
            return None
        if name not in self._gauges:
            self._gauges[name] = Gauge(name, description, labels)
        return self._gauges[name]

    def inc_counter(self, name: str, labels: dict[str, str], description: str = "") -> None:
        """Increment a counter."""
        if not self.enabled:
            return
        counter = self._get_counter(name, description, list(labels.keys()))
        if counter:
            counter.labels(**labels).inc()

    def observe_histogram(self, name: str, value: float, labels: dict[str, str], description: str = "") -> None:
        """Record a histogram observation."""
        if not self.enabled:
            return
        histogram = self._get_histogram(name, description, list(labels.keys()))
        if histogram:
            histogram.labels(**labels).observe(value)

    def set_gauge(self, name: str, value: float, labels: dict[str, str], description: str = "") -> None:
        """Set a gauge value."""
        if not self.enabled:
            return
        gauge = self._get_gauge(name, description, list(labels.keys()))
        if gauge:
            gauge.labels(**labels).set(value)


# ---------------------------------------------------------------------------
# Timer utility
# ---------------------------------------------------------------------------


@contextmanager
def timed() -> Generator[dict[str, float], None, None]:
    """Context manager that tracks elapsed time in milliseconds.

    Usage:
        with timed() as t:
            do_work()
        print(f"Took {t['elapsed_ms']:.1f}ms")
    """
    result: dict[str, float] = {"elapsed_ms": 0.0}
    start = time.monotonic()
    try:
        yield result
    finally:
        result["elapsed_ms"] = (time.monotonic() - start) * 1000
