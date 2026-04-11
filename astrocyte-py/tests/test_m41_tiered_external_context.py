"""M4.1 — TieredRetriever forwards external_context."""

from __future__ import annotations

import pytest

from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.pipeline.tiered_retrieval import TieredRetriever
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import MemoryHit, RecallRequest, VectorItem


@pytest.mark.asyncio
async def test_tiered_t4_reformulation_keeps_external_context():
    """Tier 4 RecallRequest must preserve external_context for pipeline RRF."""
    vs = InMemoryVectorStore()
    llm = MockLLMProvider()
    pipeline = PipelineOrchestrator(
        vector_store=vs,
        llm_provider=llm,
        chunk_strategy="sentence",
        max_chunk_size=512,
        semantic_overfetch=2,
    )
    emb = [0.2] * 128
    await vs.store_vectors(
        [
            VectorItem(
                id="v1",
                bank_id="b1",
                vector=emb,
                text="something about local",
            ),
        ],
    )

    ext = [
        MemoryHit(text="federated only", score=0.95, memory_id="fe1", bank_id="b1"),
    ]
    tiered = TieredRetriever(pipeline, recall_cache=None, min_results=99, max_tier=4)
    req = RecallRequest(
        query="test query",
        bank_id="b1",
        max_results=10,
        external_context=ext,
    )
    result = await tiered.retrieve(req)

    texts = {h.text for h in result.hits}
    assert "federated only" in texts
