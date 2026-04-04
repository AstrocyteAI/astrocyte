"""Tests for policy/escalation.py — circuit breaker, degraded mode."""

import time

import pytest

from astrocyte.errors import ProviderUnavailable
from astrocyte.policy.escalation import CircuitBreaker, DegradedModeHandler


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker(failure_threshold=3)
        assert cb.state == "closed"
        assert cb.is_open() is False

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"
        cb.record_failure()
        assert cb.state == "open"
        assert cb.is_open() is True

    def test_check_raises_when_open(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        with pytest.raises(ProviderUnavailable, match="circuit breaker"):
            cb.check("test-provider")

    def test_transitions_to_half_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.01)
        cb.record_failure()
        assert cb.state == "open"
        time.sleep(0.02)
        assert cb.state == "half_open"

    def test_half_open_allows_limited_calls(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.01, half_open_max_calls=2)
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == "half_open"
        cb.check("provider")  # First call OK
        cb.check("provider")  # Second call OK
        assert cb.is_open() is True  # Third would be blocked

    def test_success_closes_from_half_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.01)
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == "half_open"
        cb.record_success()
        assert cb.state == "closed"

    def test_failure_in_half_open_reopens(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.01)
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == "half_open"
        cb.record_failure()
        assert cb.state == "open"

    def test_reset(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        assert cb.state == "open"
        cb.reset()
        assert cb.state == "closed"

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        # Counter reset, so 2 more failures needed
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"  # Not yet at 3


class TestDegradedModeHandler:
    def test_empty_recall_mode(self):
        handler = DegradedModeHandler(mode="empty_recall")
        result = handler.handle_recall("test-provider")
        assert result.hits == []
        assert result.total_available == 0

    def test_error_mode_recall(self):
        handler = DegradedModeHandler(mode="error")
        with pytest.raises(ProviderUnavailable):
            handler.handle_recall("test-provider")

    def test_error_mode_retain(self):
        handler = DegradedModeHandler(mode="error")
        with pytest.raises(ProviderUnavailable):
            handler.handle_retain("test-provider")

    def test_empty_recall_retain_silent(self):
        handler = DegradedModeHandler(mode="empty_recall")
        # Should not raise
        handler.handle_retain("test-provider")
