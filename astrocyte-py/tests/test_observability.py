"""Tests for policy/observability.py — spans, structured logging, metrics."""

import json
import logging

from astrocyte.policy.observability import MetricsCollector, StructuredLogger, span, timed


class TestSpanContextManager:
    def test_no_otel_yields_noop(self):
        with span("test.span") as s:
            s.set_attribute("key", "value")  # Should not raise
            s.add_event("test_event")  # Should not raise

    def test_span_with_attributes(self):
        with span("test.span", {"bank_id": "b1", "count": 42}) as s:
            assert s is not None  # Either real span or _NoOpSpan


class TestStructuredLogger:
    def test_log_emits_json(self, caplog):
        logger = StructuredLogger(level="info")
        with caplog.at_level(logging.INFO, logger="astrocyte"):
            logger.log("test.event", bank_id="b1", data={"key": "value"})
        assert len(caplog.records) == 1
        entry = json.loads(caplog.records[0].getMessage())
        assert entry["event"] == "test.event"
        assert entry["bank_id"] == "b1"

    def test_log_omits_none_fields(self, caplog):
        logger = StructuredLogger(level="info")
        with caplog.at_level(logging.INFO, logger="astrocyte"):
            logger.log("test.event")
        entry = json.loads(caplog.records[0].getMessage())
        assert "bank_id" not in entry  # None fields omitted


class TestMetricsCollector:
    def test_disabled_collector_noop(self):
        mc = MetricsCollector(enabled=False)
        # These should not raise even when disabled
        mc.inc_counter("test_counter", {"label": "value"})
        mc.observe_histogram("test_hist", 1.5, {"label": "value"})
        mc.set_gauge("test_gauge", 42.0, {"label": "value"})

    def test_enabled_without_prometheus(self):
        # MetricsCollector with enabled=True but prometheus_client not necessarily installed
        mc = MetricsCollector(enabled=True)
        # Should not raise regardless
        mc.inc_counter("test_counter", {"label": "value"})


class TestTimed:
    def test_measures_elapsed(self):
        import time

        with timed() as t:
            time.sleep(0.01)
        assert t["elapsed_ms"] >= 5  # At least 5ms (generous tolerance)
        assert t["elapsed_ms"] < 5000  # Not absurdly long

    def test_zero_work(self):
        with timed() as t:
            pass
        assert t["elapsed_ms"] >= 0
