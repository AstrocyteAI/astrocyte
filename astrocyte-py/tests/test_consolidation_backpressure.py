"""Fire-and-forget consolidation must not starve the foreground Add path.

The defect these pin: ``_spawn_observation_consolidation`` spawned an
``asyncio.create_task`` per ``retain()`` with no ceiling, while every one of
those tasks calls ``llm_provider`` — and providers bound their own concurrency
(``ClaudeCliProvider``'s semaphore defaults to 4). Unbounded producer, bounded
consumer. Under sustained ingest the backlog grew without limit and foreground
``retain()`` calls queued behind it until the *client* gave up.

That failure was invisible from the server's side: the adapter logged 953
successful Adds and one error, while the driver recorded 13 ``ReadTimeout``s.
Measured directly, the backlog was still saturating all 4 provider slots for
421 seconds after the client had disconnected.

No test covered this path at all, which is how it shipped. These are cheap and
deterministic — they assert the ceiling, not timing.
"""

from __future__ import annotations

import asyncio

import pytest

from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider


def _spawn(pipeline: PipelineOrchestrator, n: int) -> None:
    """Drive the spawn path n times with the shape retain() passes."""
    for i in range(n):
        pipeline._spawn_observation_consolidation(
            chunks=[f"chunk {i}"],
            embeddings=[[0.0] * 8],
            memory_ids=[f"m{i}"],
            bank_id="b1",
            tags=None,
        )


class _CountingConsolidator:
    """Stub that records every run and the peak number running at once."""

    def __init__(self) -> None:
        self.runs = 0
        self.running = 0
        self.peak = 0

    async def consolidate(self, **kwargs) -> None:
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(0)
            self.runs += 1
        finally:
            self.running -= 1


def _make(**kw) -> PipelineOrchestrator:
    return PipelineOrchestrator(
        vector_store=InMemoryVectorStore(), llm_provider=MockLLMProvider(), **kw
    )


async def _drain_all(p: PipelineOrchestrator) -> None:
    """Await until nothing is in flight and nothing is deferred."""
    for _ in range(10_000):
        if not p._background_tasks and not p._deferred_consolidations:
            return
        await asyncio.gather(*list(p._background_tasks), return_exceptions=True)
    raise AssertionError("backlog never drained")


@pytest.fixture
def pipeline():
    return _make(max_pending_consolidations=4)


class TestInFlightIsBounded:
    """The original defect. Must hold under every tier."""

    async def test_pending_tasks_never_exceed_the_cap(self, pipeline):
        _spawn(pipeline, 200)
        assert len(pipeline._background_tasks) <= 4

    async def test_cap_holds_throughout_draining(self):
        p = _make(max_pending_consolidations=4)
        stub = _CountingConsolidator()
        p._observation_consolidator = stub
        _spawn(p, 200)
        await _drain_all(p)
        assert stub.peak <= 4, f"{stub.peak} consolidations ran at once (cap 4)"


class TestOverflowIsDeferredNotDropped:
    async def test_excess_goes_to_the_deferred_queue(self, pipeline):
        _spawn(pipeline, 200)
        assert len(pipeline._background_tasks) == 4
        assert len(pipeline._deferred_consolidations) == 196
        assert pipeline.consolidations_shed == 0

    async def test_every_deferred_item_eventually_runs(self):
        """The point of the change: under a burst, nothing is lost."""
        p = _make(max_pending_consolidations=4)
        stub = _CountingConsolidator()
        p._observation_consolidator = stub
        _spawn(p, 200)
        await _drain_all(p)
        assert stub.runs == 200, f"only {stub.runs}/200 consolidations ran"
        assert p.consolidations_shed == 0

    async def test_deferred_queue_is_itself_bounded(self):
        """An unbounded queue would only move the defect from tasks to memory."""
        p = _make(max_pending_consolidations=4, max_deferred_consolidations=10)
        _spawn(p, 200)
        assert len(p._background_tasks) == 4
        assert len(p._deferred_consolidations) == 10
        assert p.consolidations_shed == 186


class TestShutdown:
    async def test_shutdown_spawns_nothing_and_counts_the_undrained(self):
        """A cancelled task's done-callback must not promote deferred work."""
        p = _make(max_pending_consolidations=4)
        _spawn(p, 50)
        assert len(p._deferred_consolidations) == 46
        await p.shutdown()
        assert not p._background_tasks
        assert not p._deferred_consolidations
        assert p.consolidations_shed >= 46


class TestOptOuts:
    async def test_zero_deferred_restores_shed_on_overflow(self):
        p = _make(max_pending_consolidations=4, max_deferred_consolidations=0)
        _spawn(p, 200)
        assert p.consolidations_shed == 200 - len(p._background_tasks)

    async def test_shed_mode_recovers_capacity_after_draining(self):
        p = _make(max_pending_consolidations=4, max_deferred_consolidations=0)
        _spawn(p, 200)
        await asyncio.gather(*list(p._background_tasks), return_exceptions=True)
        assert not p._background_tasks
        before = p.consolidations_shed
        _spawn(p, 1)
        assert p.consolidations_shed == before, "should accept work again"

    async def test_default_cap_is_bounded(self):
        p = _make()
        _spawn(p, 500)
        assert len(p._background_tasks) <= p.max_pending_consolidations
        assert p.max_pending_consolidations > 0, "default must not be unbounded"
        assert p.max_deferred_consolidations > 0, "default defers overflow"

    async def test_zero_disables_the_ceiling(self):
        p = _make(max_pending_consolidations=0)
        _spawn(p, 50)
        assert len(p._background_tasks) == 50
        assert p.consolidations_shed == 0
