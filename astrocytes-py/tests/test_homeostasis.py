"""Tests for policy/homeostasis.py — rate limiter, token budget, quotas."""

import pytest

from astrocytes.errors import RateLimited
from astrocytes.policy.homeostasis import QuotaTracker, RateLimiter, count_tokens, enforce_token_budget
from astrocytes.types import MemoryHit


class TestRateLimiter:
    def test_allows_within_limit(self):
        limiter = RateLimiter(max_per_minute=60)
        # Should not raise for first call
        limiter.check_and_record("bank-1", "retain")

    def test_blocks_when_exceeded(self):
        limiter = RateLimiter(max_per_minute=2)
        limiter.check_and_record("bank-1", "retain")
        limiter.check_and_record("bank-1", "retain")
        with pytest.raises(RateLimited):
            limiter.check_and_record("bank-1", "retain")

    def test_per_bank_isolation(self):
        limiter = RateLimiter(max_per_minute=1)
        limiter.check_and_record("bank-1", "retain")
        # Different bank should be independent
        limiter.check_and_record("bank-2", "retain")

    def test_per_operation_isolation(self):
        limiter = RateLimiter(max_per_minute=1)
        limiter.check_and_record("bank-1", "retain")
        # Different operation should be independent
        limiter.check_and_record("bank-1", "recall")

    def test_rate_limited_has_retry_after(self):
        limiter = RateLimiter(max_per_minute=1)
        limiter.check_and_record("bank-1", "retain")
        with pytest.raises(RateLimited) as exc_info:
            limiter.check("bank-1", "retain")
        assert exc_info.value.retry_after_seconds is not None
        assert exc_info.value.retry_after_seconds > 0


class TestTokenBudget:
    def test_count_tokens_basic(self):
        assert count_tokens("hello world") > 0

    def test_count_tokens_empty(self):
        assert count_tokens("") >= 1  # At least 1

    def test_enforce_budget_no_truncation(self):
        hits = [MemoryHit(text="short text", score=0.9)]
        result, truncated = enforce_token_budget(hits, max_tokens=100)
        assert len(result) == 1
        assert truncated is False

    def test_enforce_budget_truncation(self):
        hits = [
            MemoryHit(text="a " * 50, score=0.9),  # ~67 tokens
            MemoryHit(text="b " * 50, score=0.8),
            MemoryHit(text="c " * 50, score=0.7),
        ]
        result, truncated = enforce_token_budget(hits, max_tokens=80)
        assert len(result) < len(hits)
        assert truncated is True

    def test_enforce_budget_empty(self):
        result, truncated = enforce_token_budget([], max_tokens=100)
        assert result == []
        assert truncated is False


class TestQuotaTracker:
    def test_allows_within_quota(self):
        tracker = QuotaTracker()
        assert tracker.check("bank-1", "retain", 10) is True

    def test_blocks_when_exceeded(self):
        tracker = QuotaTracker()
        for _ in range(5):
            tracker.record("bank-1", "retain")
        assert tracker.check("bank-1", "retain", 5) is False

    def test_no_limit(self):
        tracker = QuotaTracker()
        for _ in range(1000):
            tracker.record("bank-1", "retain")
        assert tracker.check("bank-1", "retain", None) is True

    def test_get_count(self):
        tracker = QuotaTracker()
        assert tracker.get_count("bank-1", "retain") == 0
        tracker.record("bank-1", "retain")
        tracker.record("bank-1", "retain")
        assert tracker.get_count("bank-1", "retain") == 2
