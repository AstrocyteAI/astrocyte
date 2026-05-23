"""Tests for Astrocyte class with the storage pipeline (in-memory stores).

Regression target for `provider_tier: storage` + built-in pipeline. When retain/recall
trace semantics change, update tests and `docs/_design/built-in-pipeline.md`
(storage-pipeline section).
"""

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider


def _make_tier1_astrocyte() -> tuple[Astrocyte, InMemoryVectorStore, MockLLMProvider]:
    """Create an Astrocyte with Tier 1 pipeline."""
    config = AstrocyteConfig()
    config.provider_tier = "storage"
    config.barriers.pii.mode = "disabled"
    config.escalation.degraded_mode = "error"

    brain = Astrocyte(config)
    vector_store = InMemoryVectorStore()
    llm = MockLLMProvider()
    pipeline = PipelineOrchestrator(vector_store=vector_store, llm_provider=llm)
    brain.set_pipeline(pipeline)
    return brain, vector_store, llm


class TestTier1RetainRecall:
    async def test_retain_stores_content(self):
        brain, store, _ = _make_tier1_astrocyte()
        result = await brain.retain("Calvin prefers dark mode", bank_id="bank-1")
        assert result.stored is True
        assert result.memory_id is not None

    async def test_recall_after_retain(self):
        brain, store, llm = _make_tier1_astrocyte()
        await brain.retain("Calvin prefers dark mode", bank_id="bank-1")

        # The mock LLM generates deterministic embeddings, so similar text
        # should get similar embeddings
        result = await brain.recall("Calvin prefers dark mode", bank_id="bank-1")
        assert len(result.hits) >= 1
        assert result.hits[0].score > 0

    async def test_multiple_retains(self):
        brain, store, _ = _make_tier1_astrocyte()
        await brain.retain("First memory about Python", bank_id="bank-1")
        await brain.retain("Second memory about Rust", bank_id="bank-1")
        await brain.retain("Third memory about TypeScript", bank_id="bank-1")

        result = await brain.recall("programming languages", bank_id="bank-1")
        assert result.total_available >= 1

    async def test_bank_isolation(self):
        brain, store, _ = _make_tier1_astrocyte()
        await brain.retain("Secret in bank 1", bank_id="bank-1")
        await brain.retain("Public in bank 2", bank_id="bank-2")

        result = await brain.recall("Secret", bank_id="bank-2")
        # Should NOT find bank-1 content in bank-2
        for hit in result.hits:
            assert "Secret in bank 1" not in hit.text


class TestTier1Reflect:
    async def test_reflect_via_pipeline(self):
        brain, store, llm = _make_tier1_astrocyte()
        await brain.retain("Calvin likes dark mode and Python", bank_id="bank-1")

        result = await brain.reflect("What does Calvin like?", bank_id="bank-1")
        assert result.answer  # Should get some synthesis from mock LLM

    async def test_reflect_empty_bank(self):
        brain, store, llm = _make_tier1_astrocyte()
        result = await brain.reflect("anything", bank_id="empty-bank")
        assert "don't have" in result.answer.lower() or result.answer  # Either no memories or mock response

    async def test_reflect_agentic_path_wired_when_enabled(self):
        """M20 Day 1 — verify ``config.agentic_reflect.enabled=True``
        flips the dispatcher onto the Hindsight-parity native-function-
        calling loop instead of the single-shot synthesis path.

        The MockLLMProvider does not emit ``tool_calls``, so the loop
        falls through to forced synthesis (see
        ``test_agentic_reflect.py::test_no_tool_calls_falls_back_to_forced_synthesis``).
        That's fine: this test asserts the WIRING (params present on
        the orchestrator after build) plus end-to-end ``reflect()``
        returns a valid ReflectResult — i.e. no exception from the
        agentic branch.
        """
        config = AstrocyteConfig()
        config.provider_tier = "storage"
        config.barriers.pii.mode = "disabled"
        config.escalation.degraded_mode = "error"
        config.agentic_reflect.enabled = True
        config.agentic_reflect.max_iterations = 2  # bound test cost
        brain = Astrocyte(config)
        vector_store = InMemoryVectorStore()
        llm = MockLLMProvider()
        pipeline = PipelineOrchestrator(vector_store=vector_store, llm_provider=llm)
        brain.set_pipeline(pipeline)

        # Wiring check — orchestrator picked up params from config.
        assert pipeline.agentic_reflect_params is not None
        assert pipeline.agentic_reflect_params.max_iterations == 2

        await brain.retain("Calvin likes dark mode and Python", bank_id="bank-1")
        result = await brain.reflect("What does Calvin like?", bank_id="bank-1")
        # Mock LLM emits no tool_calls → loop forces synthesis → still
        # returns a ReflectResult with a non-empty answer.
        assert result is not None
        assert isinstance(result.answer, str)


class TestTier1Pipeline:
    async def test_recall_trace_has_strategies(self):
        brain, store, _ = _make_tier1_astrocyte()
        await brain.retain("Test content", bank_id="bank-1")
        result = await brain.recall("Test", bank_id="bank-1")
        assert result.trace is not None
        assert "semantic" in result.trace.strategies_used
