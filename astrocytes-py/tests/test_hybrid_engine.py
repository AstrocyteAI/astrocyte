"""Tests for HybridEngineProvider (Tier-2 engine + Tier-1 pipeline recall merge)."""

import pytest

from astrocytes._astrocyte import Astrocyte
from astrocytes.config import AstrocyteConfig
from astrocytes.hybrid import HybridEngineProvider
from astrocytes.pipeline.orchestrator import PipelineOrchestrator
from astrocytes.testing.in_memory import InMemoryEngineProvider, InMemoryVectorStore, MockLLMProvider
from astrocytes.types import RetainRequest


def _make_brain_with_hybrid() -> tuple[Astrocyte, InMemoryEngineProvider, PipelineOrchestrator]:
    config = AstrocyteConfig()
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    llm = MockLLMProvider()
    pipeline = PipelineOrchestrator(vector_store=InMemoryVectorStore(), llm_provider=llm)
    hybrid = HybridEngineProvider(engine=engine, pipeline=pipeline, retain_target="engine")
    brain.set_engine_provider(hybrid)
    return brain, engine, pipeline


class TestHybridEngineProvider:
    async def test_recall_merges_engine_and_pipeline(self):
        brain, _engine, pipeline = _make_brain_with_hybrid()
        await brain.retain("Hosted engine fact about Redis", bank_id="b1")
        await pipeline.retain(RetainRequest(content="Pipeline fact about Postgres", bank_id="b1"))

        result = await brain.recall("fact databases", bank_id="b1")
        texts = " ".join(h.text for h in result.hits)
        assert "Redis" in texts
        assert "Postgres" in texts

    async def test_retain_target_pipeline(self):
        config = AstrocyteConfig()
        config.barriers.pii.mode = "disabled"
        brain = Astrocyte(config)
        engine = InMemoryEngineProvider()
        llm = MockLLMProvider()
        pipeline = PipelineOrchestrator(vector_store=InMemoryVectorStore(), llm_provider=llm)
        hybrid = HybridEngineProvider(engine=engine, pipeline=pipeline, retain_target="pipeline")
        brain.set_engine_provider(hybrid)

        await brain.retain("Only in pipeline store", bank_id="b2")
        result = await brain.recall("pipeline store", bank_id="b2")
        assert len(result.hits) >= 1

    async def test_capabilities_requires_backend_for_retain_target(self):
        engine = InMemoryEngineProvider()
        llm = MockLLMProvider()
        pipeline = PipelineOrchestrator(vector_store=InMemoryVectorStore(), llm_provider=llm)
        with pytest.raises(ValueError, match="retain_target='engine'"):
            HybridEngineProvider(engine=None, pipeline=pipeline, retain_target="engine")
        with pytest.raises(ValueError, match="retain_target='pipeline'"):
            HybridEngineProvider(engine=engine, pipeline=None, retain_target="pipeline")
