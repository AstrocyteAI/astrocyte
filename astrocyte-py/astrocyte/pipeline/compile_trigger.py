"""M8 W4: Threshold trigger + async compile queue.

CompileQueue monitors per-bank memory growth and staleness, enqueueing
background compile jobs when either threshold is exceeded — without ever
blocking the retain path.

Design reference: docs/_design/llm-wiki-compile.md §3.1, §8.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrocyte.pipeline.compile import CompileEngine

_logger = logging.getLogger("astrocyte.compile_trigger")

# Sentinel pushed into the queue to signal the worker to stop.
_STOP: str | None = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class CompileTriggerConfig:
    """Thresholds that govern automatic compile triggering.

    Either condition is sufficient to enqueue a compile job:

    - **Size**: ``memories_since_last_compile >= size_threshold``
    - **Staleness**: last compile is older than ``staleness_days`` AND
      ``memories_since_last_compile >= staleness_min_memories``

    Use ``size_threshold=0`` to trigger on every retain (useful for tests or
    high-frequency low-cost scenarios).
    """

    size_threshold: int = 50
    """New memories since last compile required to trigger on size alone."""

    staleness_days: float = 7.0
    """Days since last compile required for the staleness trigger."""

    staleness_min_memories: int = 10
    """Minimum new memories for the staleness condition to fire."""


# ---------------------------------------------------------------------------
# Per-bank state
# ---------------------------------------------------------------------------


@dataclass
class BankCompileState:
    """Runtime state for one bank's compile trigger."""

    bank_id: str
    memories_since_last_compile: int = 0
    last_compile_at: datetime | None = None

    def should_trigger(self, config: CompileTriggerConfig) -> bool:
        """Return True if either trigger condition is met."""
        n = self.memories_since_last_compile

        # Size trigger: enough new memories regardless of time
        if n >= config.size_threshold:
            return True

        # Staleness trigger: old enough AND at least staleness_min_memories
        if self.last_compile_at is not None and n >= config.staleness_min_memories:
            age_days = (datetime.now(UTC) - self.last_compile_at).total_seconds() / 86400.0
            if age_days >= config.staleness_days:
                return True

        return False


# ---------------------------------------------------------------------------
# Compile queue
# ---------------------------------------------------------------------------


class CompileQueue:
    """Async background queue for compile jobs.

    Usage::

        engine = CompileEngine(vs, llm, ws)
        queue = CompileQueue(engine, CompileTriggerConfig(size_threshold=10))
        await queue.start()

        # In retain path (sync-safe — notify_retain is non-blocking):
        queue.notify_retain("user-alice")

        # Graceful shutdown:
        await queue.stop()

    Call :meth:`drain` in tests to wait for all pending jobs to finish before
    making assertions::

        queue.notify_retain("bank")
        await queue.drain()
        # CompileEngine.run("bank") has now completed
    """

    def __init__(
        self,
        engine: CompileEngine,
        config: CompileTriggerConfig | None = None,
        *,
        max_queue_size: int = 100,
    ) -> None:
        self._engine = engine
        self._config = config or CompileTriggerConfig()
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=max_queue_size)
        self._pending: set[str] = set()  # banks queued but not yet running
        self._state: dict[str, BankCompileState] = {}
        self._task: asyncio.Task[None] | None = None
        self._started = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify_retain(self, bank_id: str) -> None:
        """Increment the memory counter for *bank_id* and enqueue if threshold met.

        Non-blocking and sync-safe — designed to be called from the hot retain
        path without holding any lock or awaiting anything.  Duplicate banks
        already in the queue are silently skipped so the queue never fills with
        the same bank repeated N times.
        """
        state = self._state.setdefault(bank_id, BankCompileState(bank_id=bank_id))
        state.memories_since_last_compile += 1

        if not state.should_trigger(self._config):
            return

        if bank_id in self._pending:
            # Already queued — the in-flight compile will cover these memories.
            return

        try:
            self._queue.put_nowait(bank_id)
            self._pending.add(bank_id)
            _logger.debug("Compile job enqueued for bank %r", bank_id)
        except asyncio.QueueFull:
            _logger.warning(
                "Compile queue full (%d slots); skipping bank %r. "
                "Consider increasing max_queue_size or lowering thresholds.",
                self._queue.maxsize,
                bank_id,
            )

    async def start(self) -> None:
        """Start the background worker task. Idempotent."""
        if self._started:
            return
        self._started = True
        self._task = asyncio.create_task(self._worker(), name="astrocyte.compile_worker")
        _logger.debug("Compile worker started")

    async def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the worker to stop and wait up to *timeout* seconds."""
        if not self._started or self._task is None:
            return
        self._started = False
        try:
            self._queue.put_nowait(_STOP)
        except asyncio.QueueFull:
            self._task.cancel()
        try:
            await asyncio.wait_for(self._task, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _logger.warning("Compile worker did not stop within %.1fs; cancelling.", timeout)
            self._task.cancel()
        self._task = None
        _logger.debug("Compile worker stopped")

    async def drain(self, *, timeout: float = 5.0) -> None:
        """Wait for all currently queued jobs to finish.

        Raises :exc:`asyncio.TimeoutError` if jobs do not complete within
        *timeout* seconds.  Safe to call when the worker is not started
        (returns immediately if the queue is empty).
        """
        if self._queue.empty() and not self._pending:
            return
        await asyncio.wait_for(self._queue.join(), timeout=timeout)

    @property
    def pending_banks(self) -> set[str]:
        """Banks currently in the queue awaiting compilation (snapshot)."""
        return set(self._pending)

    @property
    def state(self) -> dict[str, BankCompileState]:
        """Per-bank compile state, keyed by bank_id (snapshot)."""
        return dict(self._state)

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    async def _worker(self) -> None:
        """Process compile jobs from the queue until a stop sentinel is received."""
        _logger.debug("Compile worker loop started")
        while True:
            bank_id = await self._queue.get()
            try:
                if bank_id is _STOP:
                    _logger.debug("Compile worker received stop signal")
                    break

                self._pending.discard(bank_id)
                _logger.info("Running background compile for bank %r", bank_id)
                try:
                    result = await self._engine.run(bank_id)
                    _logger.info(
                        "Background compile done for bank %r: "
                        "%d created, %d updated, %d noise, %d tokens, %dms",
                        bank_id,
                        result.pages_created,
                        result.pages_updated,
                        result.noise_memories,
                        result.tokens_used,
                        result.elapsed_ms,
                    )
                    if result.error:
                        _logger.error(
                            "Background compile for bank %r reported error: %s",
                            bank_id,
                            result.error,
                        )
                    # Reset counter; record compile time
                    state = self._state.setdefault(bank_id, BankCompileState(bank_id=bank_id))
                    state.memories_since_last_compile = 0
                    state.last_compile_at = datetime.now(UTC)

                except Exception:
                    _logger.exception(
                        "Background compile for bank %r raised unexpectedly; skipping.",
                        bank_id,
                    )
            finally:
                self._queue.task_done()
