"""Tiered retrieval with shared ``full_recall`` hook: pipeline vs hybrid escalation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.hybrid import HybridEngineProvider
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.pipeline.tiered_retrieval import TieredRetriever
from astrocyte.testing.in_memory import InMemoryEngineProvider, InMemoryVectorStore, MockLLMProvider
from astrocyte.types import RecallRequest, RecallResult


@pytest.mark.asyncio
async def test_tier3_uses_injected_full_recall_not_pipeline_recall() -> None:
    """When ``full_recall`` is set, tier 3+ uses it; default pipeline.recall is not called."""
    llm = MockLLMProvider()
    pipeline = PipelineOrchestrator(vector_store=InMemoryVectorStore(), llm_provider=llm)
    mock_full = AsyncMock(
        return_value=RecallResult(hits=[], total_available=0, truncated=False),
    )
    tiered = TieredRetriever(
        pipeline,
        recall_cache=None,
        max_tier=3,
        min_results=3,
        full_recall=mock_full,
    )
    with patch.object(pipeline, "recall", new_callable=AsyncMock) as pipe_recall:
        await tiered.retrieve(RecallRequest(query="q", bank_id="b1"))
    mock_full.assert_called_once()
    pipe_recall.assert_not_called()


@pytest.mark.asyncio
async def test_do_recall_hybrid_full_recall_routes_through_tiered_retriever() -> None:
    cfg = AstrocyteConfig()
    cfg.barriers.pii.mode = "disabled"
    cfg.tiered_retrieval.enabled = True
    cfg.tiered_retrieval.full_recall = "hybrid"
    cfg.tiered_retrieval.max_tier = 3

    brain = Astrocyte(cfg)
    engine = InMemoryEngineProvider()
    llm = MockLLMProvider()
    pipeline = PipelineOrchestrator(vector_store=InMemoryVectorStore(), llm_provider=llm)
    hybrid = HybridEngineProvider(engine=engine, pipeline=pipeline, retain_target="engine")
    brain.set_engine_provider(hybrid)
    brain.set_pipeline(pipeline)

    assert brain._tiered_retriever is not None

    mock_result = RecallResult(hits=[], total_available=0, truncated=False)
    with patch.object(
        brain._tiered_retriever,
        "retrieve",
        new_callable=AsyncMock,
        return_value=mock_result,
    ) as tr:
        out = await brain._do_recall(RecallRequest(query="hello", bank_id="b1"))
    tr.assert_called_once()
    assert out is mock_result


@pytest.mark.asyncio
async def test_hybrid_full_recall_requires_hybrid_provider() -> None:
    """``full_recall: hybrid`` without HybridEngineProvider leaves tiered disabled."""
    cfg = AstrocyteConfig()
    cfg.barriers.pii.mode = "disabled"
    cfg.tiered_retrieval.enabled = True
    cfg.tiered_retrieval.full_recall = "hybrid"
    cfg.tiered_retrieval.max_tier = 3

    brain = Astrocyte(cfg)
    engine = InMemoryEngineProvider()
    llm = MockLLMProvider()
    pipeline = PipelineOrchestrator(vector_store=InMemoryVectorStore(), llm_provider=llm)
    brain.set_engine_provider(engine)
    brain.set_pipeline(pipeline)

    assert brain._tiered_retriever is None
