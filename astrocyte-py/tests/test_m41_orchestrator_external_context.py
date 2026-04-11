"""M4.1 — external / proxy hits merge into RRF (orchestrator)."""

from __future__ import annotations

import pytest

from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import MemoryHit, RecallRequest, VectorItem


@pytest.mark.asyncio
async def test_recall_merges_external_context_into_fusion():
    """Local + external lists participate in RRF; external hits can surface in results."""
    vs = InMemoryVectorStore()
    llm = MockLLMProvider()
    pipeline = PipelineOrchestrator(
        vector_store=vs,
        llm_provider=llm,
        chunk_strategy="sentence",
        max_chunk_size=512,
        semantic_overfetch=2,
    )
    # One local vector so semantic search returns something
    emb = [0.1] * 128
    await vs.store_vectors(
        [
            VectorItem(
                id="local-1",
                bank_id="b1",
                vector=emb,
                text="local memory about cats",
            ),
        ],
    )

    ext = [
        MemoryHit(
            text="external API hit about dogs",
            score=0.99,
            memory_id="ext-1",
            bank_id="b1",
        ),
    ]
    req = RecallRequest(
        query="cats and dogs",
        bank_id="b1",
        max_results=10,
        external_context=ext,
    )
    result = await pipeline.recall(req)

    texts = {h.text for h in result.hits}
    assert "external API hit about dogs" in texts
    trace = result.trace
    assert trace is not None
    assert trace.strategies_used is not None
    assert "proxy" in trace.strategies_used
