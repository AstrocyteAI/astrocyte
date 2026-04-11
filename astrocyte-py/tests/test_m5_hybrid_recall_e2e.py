"""M5 — hybrid multi-strategy recall (semantic + graph) with in-memory stores.

Production DB adapters are separate packages; this E2E validates ``parallel_retrieve`` fusion
via :class:`~astrocyte.pipeline.orchestrator.PipelineOrchestrator` and :class:`Astrocyte`.
"""

from __future__ import annotations

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryGraphStore, InMemoryVectorStore, MockLLMProvider


@pytest.mark.asyncio
async def test_pipeline_recall_runs_semantic_and_graph_strategies() -> None:
    vs = InMemoryVectorStore()
    gs = InMemoryGraphStore()
    llm = MockLLMProvider()
    pipeline = PipelineOrchestrator(vector_store=vs, graph_store=gs, llm_provider=llm)

    config = AstrocyteConfig()
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    brain.set_pipeline(pipeline)

    await brain.retain(
        "Planning meeting notes: Test Entity owns the launch checklist.",
        bank_id="m5-bank",
    )
    result = await brain.recall("Test Entity launch", bank_id="m5-bank", max_results=10)

    assert result.trace is not None
    assert "semantic" in result.trace.strategies_used
    assert "graph" in result.trace.strategies_used
    assert len(result.hits) >= 1
