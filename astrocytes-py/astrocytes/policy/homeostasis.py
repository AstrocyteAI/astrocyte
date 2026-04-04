"""Homeostasis policies — rate limiting, token budgets, quotas.

All functions are sync (Rust migration candidates).
See docs/_design/policy-layer.md section 1 and docs/_design/implementation-language-strategy.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from astrocytes.errors import RateLimited
from astrocytes.types import MemoryHit

# ---------------------------------------------------------------------------
# Rate limiter (token bucket algorithm)
# ---------------------------------------------------------------------------


@dataclass
class _BucketState:
    tokens: float
    last_refill: float


class RateLimiter:
    """Per-bank, per-operation token bucket rate limiter.

    Sync, stateful, self-contained — Rust migration candidate.
    """

    def __init__(self, max_per_minute: int) -> None:
        self._max_per_minute = max_per_minute
        self._tokens_per_second = max_per_minute / 60.0
        self._max_tokens = float(max_per_minute)
        self._buckets: dict[str, _BucketState] = {}

    def _get_bucket(self, key: str) -> _BucketState:
        if key not in self._buckets:
            self._buckets[key] = _BucketState(tokens=self._max_tokens, last_refill=time.monotonic())
        return self._buckets[key]

    def _refill(self, bucket: _BucketState) -> None:
        now = time.monotonic()
        elapsed = now - bucket.last_refill
        bucket.tokens = min(self._max_tokens, bucket.tokens + elapsed * self._tokens_per_second)
        bucket.last_refill = now

    def check(self, bank_id: str, operation: str) -> None:
        """Check rate limit. Raises RateLimited if exceeded."""
        key = f"{bank_id}:{operation}"
        bucket = self._get_bucket(key)
        self._refill(bucket)

        if bucket.tokens < 1.0:
            retry_after = (1.0 - bucket.tokens) / self._tokens_per_second
            raise RateLimited(bank_id=bank_id, operation=operation, retry_after_seconds=retry_after)

    def record(self, bank_id: str, operation: str) -> None:
        """Record a successful operation (consume one token)."""
        key = f"{bank_id}:{operation}"
        bucket = self._get_bucket(key)
        self._refill(bucket)
        bucket.tokens = max(0.0, bucket.tokens - 1.0)

    def check_and_record(self, bank_id: str, operation: str) -> None:
        """Check and consume in one call."""
        self.check(bank_id, operation)
        self.record(bank_id, operation)


# ---------------------------------------------------------------------------
# Token budget enforcement
# ---------------------------------------------------------------------------


def count_tokens(text: str) -> int:
    """Approximate token count via word splitting.

    Phase 1: simple whitespace split (~0.75 tokens per word).
    Phase 2 (Rust): tiktoken-compatible BPE tokenizer.
    """
    return max(1, int(len(text.split()) * 1.33))


def enforce_token_budget(hits: list[MemoryHit], max_tokens: int) -> tuple[list[MemoryHit], bool]:
    """Truncate hit list to fit within token budget.

    Returns (truncated_hits, was_truncated).
    Sync, pure computation — Rust migration candidate.
    """
    result: list[MemoryHit] = []
    total = 0
    truncated = False

    for hit in hits:
        tokens = count_tokens(hit.text)
        if total + tokens > max_tokens:
            truncated = True
            break
        result.append(hit)
        total += tokens

    return result, truncated


# ---------------------------------------------------------------------------
# Quota tracker (daily counters)
# ---------------------------------------------------------------------------


class QuotaTracker:
    """Per-bank daily quota tracking.

    Sync, self-contained — Rust migration candidate.
    Resets are time-based (tracks day boundaries).
    """

    def __init__(self) -> None:
        # {bank_id:operation -> (count, day_number)}
        self._counters: dict[str, tuple[int, int]] = {}

    @staticmethod
    def _today() -> int:
        return int(time.time() // 86400)

    def check(self, bank_id: str, operation: str, limit: int | None) -> bool:
        """Check if quota allows the operation. Returns True if allowed."""
        if limit is None:
            return True

        key = f"{bank_id}:{operation}"
        today = self._today()

        count, day = self._counters.get(key, (0, today))
        if day != today:
            count = 0  # Reset on new day

        return count < limit

    def record(self, bank_id: str, operation: str) -> None:
        """Record one operation against the quota."""
        key = f"{bank_id}:{operation}"
        today = self._today()

        count, day = self._counters.get(key, (0, today))
        if day != today:
            count = 0
            day = today

        self._counters[key] = (count + 1, day)

    def get_count(self, bank_id: str, operation: str) -> int:
        """Get current count for a bank+operation."""
        key = f"{bank_id}:{operation}"
        today = self._today()
        count, day = self._counters.get(key, (0, today))
        if day != today:
            return 0
        return count
