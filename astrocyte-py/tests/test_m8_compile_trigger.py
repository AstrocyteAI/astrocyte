"""M8 W4: Threshold trigger + async compile queue — unit tests.

Tests cover:
- CompileTriggerConfig defaults and custom values
- BankCompileState.should_trigger: size threshold, staleness, edge cases
- CompileQueue:
    - notify_retain increments counter
    - no trigger below threshold
    - triggers at size threshold
    - deduplication: same bank queued only once
    - background worker processes jobs (start + drain)
    - state reset after compile (counter → 0, last_compile_at set)
    - staleness trigger
    - staleness below min_memories (no trigger)
    - worker failure does not crash the queue
    - stop is graceful
- brain.set_compile_queue / notify_retain wired through _astrocyte.Astrocyte
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrocyte.pipeline.compile_trigger import (
    BankCompileState,
    CompileQueue,
    CompileTriggerConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**kwargs) -> CompileTriggerConfig:
    return CompileTriggerConfig(**kwargs)


def _state(bank_id: str = "bank1", n: int = 0, last: datetime | None = None) -> BankCompileState:
    return BankCompileState(bank_id=bank_id, memories_since_last_compile=n, last_compile_at=last)


def _mock_engine() -> MagicMock:
    """Return a mock CompileEngine whose run() completes successfully."""
    from astrocyte.types import CompileResult

    engine = MagicMock()
    engine.run = AsyncMock(
        return_value=CompileResult(
            bank_id="bank1",
            scopes_compiled=["scope-a"],
            pages_created=1,
            pages_updated=0,
            noise_memories=0,
            tokens_used=100,
            elapsed_ms=50,
        )
    )
    return engine


def _queue(engine=None, **cfg_kwargs) -> CompileQueue:
    return CompileQueue(engine or _mock_engine(), _config(**cfg_kwargs))


# ---------------------------------------------------------------------------
# CompileTriggerConfig
# ---------------------------------------------------------------------------


class TestCompileTriggerConfig:
    def test_defaults(self):
        c = CompileTriggerConfig()
        assert c.size_threshold == 50
        assert c.staleness_days == 7.0
        assert c.staleness_min_memories == 10

    def test_custom_values(self):
        c = CompileTriggerConfig(size_threshold=5, staleness_days=1.0, staleness_min_memories=2)
        assert c.size_threshold == 5
        assert c.staleness_days == 1.0
        assert c.staleness_min_memories == 2


# ---------------------------------------------------------------------------
# BankCompileState.should_trigger
# ---------------------------------------------------------------------------


class TestBankCompileStateShouldTrigger:
    def test_size_threshold_exact(self):
        cfg = _config(size_threshold=10)
        s = _state(n=10)
        assert s.should_trigger(cfg) is True

    def test_size_threshold_exceeded(self):
        cfg = _config(size_threshold=10)
        s = _state(n=100)
        assert s.should_trigger(cfg) is True

    def test_size_below_threshold(self):
        cfg = _config(size_threshold=10)
        s = _state(n=9)
        assert s.should_trigger(cfg) is False

    def test_staleness_trigger_fires(self):
        cfg = _config(staleness_days=1.0, staleness_min_memories=5, size_threshold=100)
        old = datetime.now(UTC) - timedelta(days=2)
        s = _state(n=5, last=old)
        assert s.should_trigger(cfg) is True

    def test_staleness_trigger_below_min_memories(self):
        cfg = _config(staleness_days=1.0, staleness_min_memories=5, size_threshold=100)
        old = datetime.now(UTC) - timedelta(days=2)
        s = _state(n=4, last=old)  # only 4, need 5
        assert s.should_trigger(cfg) is False

    def test_staleness_not_old_enough(self):
        cfg = _config(staleness_days=7.0, staleness_min_memories=5, size_threshold=100)
        recent = datetime.now(UTC) - timedelta(days=1)  # only 1 day old
        s = _state(n=10, last=recent)
        assert s.should_trigger(cfg) is False

    def test_no_last_compile_staleness_does_not_fire(self):
        """Without a previous compile, staleness trigger does not fire (size is the guard)."""
        cfg = _config(staleness_days=0.0, staleness_min_memories=1, size_threshold=100)
        s = _state(n=50, last=None)
        # last_compile_at is None → staleness branch skipped
        assert s.should_trigger(cfg) is False

    def test_zero_size_threshold_always_triggers(self):
        cfg = _config(size_threshold=0)
        s = _state(n=0)
        assert s.should_trigger(cfg) is True


# ---------------------------------------------------------------------------
# CompileQueue — synchronous state tracking
# ---------------------------------------------------------------------------


class TestCompileQueueState:
    def test_notify_increments_counter(self):
        q = _queue(size_threshold=100)
        q.notify_retain("bank1")
        q.notify_retain("bank1")
        assert q.state["bank1"].memories_since_last_compile == 2

    def test_no_pending_below_threshold(self):
        q = _queue(size_threshold=10)
        for _ in range(9):
            q.notify_retain("bank1")
        assert "bank1" not in q.pending_banks

    def test_pending_at_threshold(self):
        q = _queue(size_threshold=3)
        for _ in range(3):
            q.notify_retain("bank1")
        assert "bank1" in q.pending_banks

    def test_deduplication_same_bank(self):
        q = _queue(size_threshold=1)
        q.notify_retain("bank1")
        q.notify_retain("bank1")  # second notify — bank already pending
        assert q._queue.qsize() == 1  # only one job enqueued

    def test_different_banks_both_queued(self):
        q = _queue(size_threshold=1)
        q.notify_retain("bank1")
        q.notify_retain("bank2")
        assert "bank1" in q.pending_banks
        assert "bank2" in q.pending_banks
        assert q._queue.qsize() == 2


# ---------------------------------------------------------------------------
# CompileQueue — async worker
# ---------------------------------------------------------------------------


class TestCompileQueueWorker:
    @pytest.mark.asyncio
    async def test_worker_processes_job(self):
        engine = _mock_engine()
        engine.run.return_value.bank_id = "bank1"
        q = CompileQueue(engine, _config(size_threshold=2))

        await q.start()
        q.notify_retain("bank1")
        q.notify_retain("bank1")  # triggers at size=2
        await q.drain(timeout=2.0)
        await q.stop()

        engine.run.assert_awaited_once_with("bank1")

    @pytest.mark.asyncio
    async def test_state_reset_after_compile(self):
        engine = _mock_engine()
        q = CompileQueue(engine, _config(size_threshold=3))

        await q.start()
        for _ in range(3):
            q.notify_retain("bank1")
        await q.drain(timeout=2.0)
        await q.stop()

        state = q.state["bank1"]
        assert state.memories_since_last_compile == 0
        assert state.last_compile_at is not None

    @pytest.mark.asyncio
    async def test_multiple_banks_compiled(self):
        engine = _mock_engine()
        q = CompileQueue(engine, _config(size_threshold=1))

        await q.start()
        q.notify_retain("bank-a")
        q.notify_retain("bank-b")
        await q.drain(timeout=2.0)
        await q.stop()

        assert engine.run.await_count == 2
        called_banks = {call.args[0] for call in engine.run.await_args_list}
        assert called_banks == {"bank-a", "bank-b"}

    @pytest.mark.asyncio
    async def test_worker_failure_does_not_crash(self):
        """A failed compile should be logged but the worker continues."""
        engine = MagicMock()
        engine.run = AsyncMock(side_effect=RuntimeError("boom"))

        q = CompileQueue(engine, _config(size_threshold=1))
        await q.start()

        q.notify_retain("failing-bank")
        await q.drain(timeout=2.0)

        # Queue still processes subsequent jobs after a failure
        q.notify_retain("another-bank")
        # Patch a successful result for the second call
        from astrocyte.types import CompileResult

        engine.run.side_effect = None
        engine.run.return_value = CompileResult(
            bank_id="another-bank",
            scopes_compiled=[],
            pages_created=0,
            pages_updated=0,
            noise_memories=0,
            tokens_used=0,
            elapsed_ms=0,
        )
        await q.drain(timeout=2.0)
        await q.stop()

        # Both jobs attempted; no crash
        assert engine.run.await_count == 2

    @pytest.mark.asyncio
    async def test_stop_is_graceful(self):
        engine = _mock_engine()
        q = CompileQueue(engine, _config(size_threshold=100))
        await q.start()
        await q.stop(timeout=1.0)
        # No exception; task is gone
        assert q._task is None

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self):
        engine = _mock_engine()
        q = CompileQueue(engine, _config(size_threshold=100))
        await q.start()
        task1 = q._task
        await q.start()  # second start — should no-op
        assert q._task is task1
        await q.stop()

    @pytest.mark.asyncio
    async def test_drain_returns_immediately_when_empty(self):
        q = _queue(size_threshold=100)
        await q.drain(timeout=0.1)  # Should not raise

    @pytest.mark.asyncio
    async def test_staleness_trigger_via_queue(self):
        """Staleness trigger fires when last_compile is old enough."""
        engine = _mock_engine()
        q = CompileQueue(
            engine,
            _config(size_threshold=100, staleness_days=1.0, staleness_min_memories=2),
        )

        # Simulate a previous compile 2 days ago
        q._state["bank1"] = BankCompileState(
            bank_id="bank1",
            memories_since_last_compile=0,
            last_compile_at=datetime.now(UTC) - timedelta(days=2),
        )

        await q.start()
        # Two notifications to meet staleness_min_memories=2 (size_threshold=100 not met)
        q.notify_retain("bank1")
        q.notify_retain("bank1")
        await q.drain(timeout=2.0)
        await q.stop()

        engine.run.assert_awaited_once_with("bank1")


# ---------------------------------------------------------------------------
# Astrocyte integration
# ---------------------------------------------------------------------------


class TestAstrocyteCompileQueueIntegration:
    @pytest.mark.asyncio
    async def test_set_compile_queue_accepted(self):
        from astrocyte._astrocyte import Astrocyte
        from astrocyte.config import AstrocyteConfig

        brain = Astrocyte(AstrocyteConfig())
        q = _queue(size_threshold=100)
        brain.set_compile_queue(q)
        assert brain._compile_queue is q

    @pytest.mark.asyncio
    async def test_notify_called_after_successful_retain(self):
        from astrocyte._astrocyte import Astrocyte
        from astrocyte.config import AstrocyteConfig
        from astrocyte.pipeline.orchestrator import PipelineOrchestrator
        from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider

        brain = Astrocyte(AstrocyteConfig())
        brain.set_pipeline(PipelineOrchestrator(InMemoryVectorStore(), MockLLMProvider()))

        notified: list[str] = []

        class _SpyQueue:
            def notify_retain(self, bank_id: str) -> None:
                notified.append(bank_id)

        brain.set_compile_queue(_SpyQueue())

        await brain.retain("Alice works at Meta.", bank_id="test-bank")

        assert notified == ["test-bank"]

    @pytest.mark.asyncio
    async def test_notify_not_called_after_failed_retain(self):
        """Failed retain (empty content) must not trigger the compile queue."""
        from astrocyte._astrocyte import Astrocyte
        from astrocyte.config import AstrocyteConfig
        from astrocyte.pipeline.orchestrator import PipelineOrchestrator
        from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider

        brain = Astrocyte(AstrocyteConfig())
        brain.set_pipeline(PipelineOrchestrator(InMemoryVectorStore(), MockLLMProvider()))

        notified: list[str] = []

        class _SpyQueue:
            def notify_retain(self, bank_id: str) -> None:
                notified.append(bank_id)

        brain.set_compile_queue(_SpyQueue())

        result = await brain.retain("", bank_id="test-bank")
        assert not result.stored
        assert notified == []  # queue must NOT have been notified
