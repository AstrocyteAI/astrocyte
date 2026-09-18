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


@pytest.fixture
def pipeline():
    return PipelineOrchestrator(
        vector_store=InMemoryVectorStore(),
        llm_provider=MockLLMProvider(),
        max_pending_consolidations=4,
    )


class TestBacklogIsBounded:
    async def test_pending_tasks_never_exceed_the_cap(self, pipeline):
        """The core invariant. Without the cap this reaches 200."""
        _spawn(pipeline, 200)
        assert len(pipeline._background_tasks) <= 4

    async def test_excess_is_shed_and_counted(self, pipeline):
        """Shedding must be observable — a silent drop is its own bug."""
        _spawn(pipeline, 200)
        assert pipeline.consolidations_shed == 200 - len(pipeline._background_tasks)

    async def test_work_resumes_once_the_backlog_drains(self, pipeline):
        """Shedding is backpressure, not a latch: capacity must come back."""
        _spawn(pipeline, 200)
        assert pipeline.consolidations_shed > 0

        await asyncio.gather(*list(pipeline._background_tasks), return_exceptions=True)
        assert not pipeline._background_tasks

        shed_before = pipeline.consolidations_shed
        _spawn(pipeline, 1)
        assert pipeline.consolidations_shed == shed_before, "should accept work again"
        assert len(pipeline._background_tasks) == 1


class TestDefaultsAndOptOut:
    async def test_default_cap_is_bounded(self):
        """A deployment that configures nothing must still be protected."""
        p = PipelineOrchestrator(
            vector_store=InMemoryVectorStore(), llm_provider=MockLLMProvider()
        )
        _spawn(p, 500)
        assert len(p._background_tasks) <= p.max_pending_consolidations
        assert p.max_pending_consolidations > 0, "default must not be unbounded"

    async def test_zero_disables_the_ceiling(self):
        """0 is the documented escape hatch — pinned so it stays deliberate."""
        p = PipelineOrchestrator(
            vector_store=InMemoryVectorStore(),
            llm_provider=MockLLMProvider(),
            max_pending_consolidations=0,
        )
        _spawn(p, 50)
        assert len(p._background_tasks) == 50
        assert p.consolidations_shed == 0
