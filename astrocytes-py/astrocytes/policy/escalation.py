"""Escalation policies — circuit breaker, degraded mode.

All functions are sync (Rust migration candidates).
See docs/_design/policy-layer.md section 4.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from astrocytes.errors import ProviderUnavailable
from astrocytes.types import RecallResult

# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


@dataclass
class CircuitBreakerState:
    state: Literal["closed", "open", "half_open"] = "closed"
    failure_count: int = 0
    last_failure_time: float = 0.0
    half_open_calls: int = 0


class CircuitBreaker:
    """Circuit breaker for provider calls.

    States: closed → open (after failures) → half_open (after timeout) → closed (after success).
    Sync, self-contained — Rust migration candidate.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        half_open_max_calls: int = 2,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout_seconds
        self.half_open_max_calls = half_open_max_calls
        self._state = CircuitBreakerState()

    @property
    def state(self) -> Literal["closed", "open", "half_open"]:
        """Current state, with automatic open → half_open transition on timeout."""
        if self._state.state == "open":
            elapsed = time.monotonic() - self._state.last_failure_time
            if elapsed >= self.recovery_timeout:
                self._state.state = "half_open"
                self._state.half_open_calls = 0
        return self._state.state

    def is_open(self) -> bool:
        """Check if circuit breaker blocks calls."""
        current = self.state  # Triggers timeout-based transition
        if current == "open":
            return True
        if current == "half_open" and self._state.half_open_calls >= self.half_open_max_calls:
            return True
        return False

    def check(self, provider: str) -> None:
        """Check if call is allowed. Raises ProviderUnavailable if blocked."""
        if self.is_open():
            raise ProviderUnavailable(provider, reason="circuit breaker open")

        if self.state == "half_open":
            self._state.half_open_calls += 1

    def record_success(self) -> None:
        """Record a successful call. Resets breaker to closed."""
        self._state.state = "closed"
        self._state.failure_count = 0
        self._state.half_open_calls = 0

    def record_failure(self) -> None:
        """Record a failed call. May trip breaker to open."""
        self._state.failure_count += 1
        self._state.last_failure_time = time.monotonic()

        if self._state.failure_count >= self.failure_threshold:
            self._state.state = "open"
        elif self._state.state == "half_open":
            # Any failure in half_open trips back to open
            self._state.state = "open"

    def reset(self) -> None:
        """Force reset to closed state."""
        self._state = CircuitBreakerState()


# ---------------------------------------------------------------------------
# Degraded mode handler
# ---------------------------------------------------------------------------


class DegradedModeHandler:
    """Handle operations when provider is unavailable.

    Sync, stateless — Rust migration candidate.
    """

    def __init__(self, mode: str = "empty_recall") -> None:
        self.mode = mode  # "empty_recall" | "error" | "cache"

    def handle_recall(self, provider: str) -> RecallResult:
        """Handle a recall when provider is unavailable."""
        if self.mode == "error":
            raise ProviderUnavailable(provider, reason="degraded mode: error")

        if self.mode == "empty_recall":
            return RecallResult(
                hits=[],
                total_available=0,
                truncated=False,
            )

        # "cache" mode — not implemented in Phase 1
        return RecallResult(
            hits=[],
            total_available=0,
            truncated=False,
        )

    def handle_retain(self, provider: str) -> None:
        """Handle a retain when provider is unavailable."""
        if self.mode == "error":
            raise ProviderUnavailable(provider, reason="degraded mode: error")
        # For empty_recall mode: retain is silently dropped (could queue for retry)
