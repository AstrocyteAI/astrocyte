"""Tests for R1: Hypothetical Document Embedding (HyDE).

Covers:
- hyde.generate_hyde_vector: success path, LLM failure fallback, embed failure fallback
- parallel_retrieve: hyde strategy added when hyde_vector provided, absent when None
- Orchestrator: enable_hyde=False (default) never calls generate_hyde_vector,
  enable_hyde=True calls it and passes result to retrieval
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline.hyde import _generate_hypothetical, generate_hyde_vector
from astrocyte.pipeline.retrieval import parallel_retrieve
from astrocyte.testing.in_memory import InMemoryVectorStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockLLM:
    """Minimal LLM stub that returns a fixed response."""

    def __init__(self, response: str = "Alice works at Acme Corp as a software engineer."):
        self.response = response
        self.calls: list[list[dict]] = []

    async def complete(self, messages, **kwargs) -> str:
        self.calls.append(messages)
        return self.response

    async def embed(self, texts, **kwargs) -> list[list[float]]:
        # Return a simple deterministic embedding per text.
        return [[float(ord(c)) / 1000 for c in text[:8].ljust(8)] for text in texts]


class _FailingLLM(_MockLLM):
    async def complete(self, messages, **kwargs) -> str:
        raise RuntimeError("LLM unavailable")


class _FailingEmbedLLM(_MockLLM):
    async def embed(self, texts, **kwargs) -> list[list[float]]:
        raise RuntimeError("Embed service down")


# ---------------------------------------------------------------------------
# generate_hyde_vector
# ---------------------------------------------------------------------------

class TestGenerateHydeVector:
    @pytest.mark.asyncio
    async def test_success_returns_vector(self):
        llm = _MockLLM("Alice works at Acme Corp.")
        vec = await generate_hyde_vector("Where does Alice work?", llm)
        assert vec is not None
        assert isinstance(vec, list)
        assert all(isinstance(v, float) for v in vec)

    @pytest.mark.asyncio
    async def test_llm_failure_returns_none(self):
        llm = _FailingLLM()
        vec = await generate_hyde_vector("Where does Alice work?", llm)
        assert vec is None

    @pytest.mark.asyncio
    async def test_embed_failure_returns_none(self):
        llm = _FailingEmbedLLM()
        vec = await generate_hyde_vector("Where does Alice work?", llm)
        assert vec is None

    @pytest.mark.asyncio
    async def test_empty_response_returns_none(self):
        llm = _MockLLM("")
        vec = await generate_hyde_vector("Where does Alice work?", llm)
        assert vec is None

    @pytest.mark.asyncio
    async def test_whitespace_only_response_returns_none(self):
        llm = _MockLLM("   \n  ")
        vec = await generate_hyde_vector("Where does Alice work?", llm)
        assert vec is None

    @pytest.mark.asyncio
    async def test_llm_receives_correct_prompt_structure(self):
        llm = _MockLLM("hypothetical answer")
        await generate_hyde_vector("test query", llm)
        assert len(llm.calls) == 1
        messages = llm.calls[0]
        roles = [m["role"] for m in messages]
        assert roles == ["system", "user"]
        assert "test query" in messages[-1]["content"]

    @pytest.mark.asyncio
    async def test_generate_hypothetical_passes_query_to_llm(self):
        llm = _MockLLM("some fact")
        result = await _generate_hypothetical("my query", llm)
        assert result == "some fact"
        assert llm.calls[0][-1]["content"] == "my query"


# ---------------------------------------------------------------------------
# parallel_retrieve — hyde strategy
# ---------------------------------------------------------------------------

class TestParallelRetrieveHyde:
    @pytest.mark.asyncio
    async def test_no_hyde_vector_no_hyde_strategy(self):
        vs = InMemoryVectorStore()
        results = await parallel_retrieve(
            query_vector=[0.1, 0.2, 0.3],
            query_text="test",
            bank_id="bank1",
            vector_store=vs,
            hyde_vector=None,
        )
        assert "hyde" not in results
        assert "semantic" in results

    @pytest.mark.asyncio
    async def test_hyde_vector_adds_hyde_strategy(self):
        vs = InMemoryVectorStore()
        results = await parallel_retrieve(
            query_vector=[0.1, 0.2, 0.3],
            query_text="test",
            bank_id="bank1",
            vector_store=vs,
            hyde_vector=[0.4, 0.5, 0.6],
        )
        assert "hyde" in results
        assert "semantic" in results

    @pytest.mark.asyncio
    async def test_hyde_results_are_scored_items(self):
        from astrocyte.pipeline.fusion import ScoredItem
        vs = InMemoryVectorStore()
        results = await parallel_retrieve(
            query_vector=[0.1, 0.2, 0.3],
            query_text="test",
            bank_id="bank1",
            vector_store=vs,
            hyde_vector=[0.4, 0.5, 0.6],
        )
        assert isinstance(results["hyde"], list)
        for item in results["hyde"]:
            assert isinstance(item, ScoredItem)


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------

class TestOrchestratorHyde:
    @pytest.mark.asyncio
    async def test_hyde_disabled_by_default(self):
        """enable_hyde=False: orchestrator stores the flag correctly."""
        from astrocyte.pipeline.orchestrator import PipelineOrchestrator as Orchestrator
        vs = InMemoryVectorStore()
        llm = _MockLLM("hypothetical")
        orch = Orchestrator(vector_store=vs, llm_provider=llm, enable_hyde=False)
        assert orch.enable_hyde is False

    @pytest.mark.asyncio
    async def test_hyde_enabled_flag_stored(self):
        from astrocyte.pipeline.orchestrator import PipelineOrchestrator as Orchestrator
        vs = InMemoryVectorStore()
        llm = _MockLLM("hypothetical")
        orch = Orchestrator(vector_store=vs, llm_provider=llm, enable_hyde=True)
        assert orch.enable_hyde is True

    @pytest.mark.asyncio
    async def test_hyde_disabled_no_complete_call(self):
        """With enable_hyde=False, recall never calls complete() for HyDE."""
        from astrocyte.pipeline.orchestrator import PipelineOrchestrator as Orchestrator
        from astrocyte.types import RecallRequest

        vs = InMemoryVectorStore()
        llm = _MockLLM("hypothetical")
        orch = Orchestrator(vector_store=vs, llm_provider=llm, enable_hyde=False)

        await orch.recall(RecallRequest(query="Where does Alice work?", bank_id="bank1"))
        # No system-prompt calls (HyDE uses a system prompt; embed does not)
        complete_calls = [c for c in llm.calls if c and c[0]["role"] == "system"]
        assert len(complete_calls) == 0

    @pytest.mark.asyncio
    async def test_hyde_enabled_makes_complete_call(self):
        """With enable_hyde=True, recall calls complete() for hypothetical generation."""
        from astrocyte.pipeline.orchestrator import PipelineOrchestrator as Orchestrator
        from astrocyte.types import RecallRequest

        vs = InMemoryVectorStore()
        llm = _MockLLM("Alice works at Acme Corp.")
        orch = Orchestrator(vector_store=vs, llm_provider=llm, enable_hyde=True)

        await orch.recall(RecallRequest(query="Where does Alice work?", bank_id="bank1"))
        complete_calls = [c for c in llm.calls if c and c[0]["role"] == "system"]
        assert len(complete_calls) >= 1

    @pytest.mark.asyncio
    async def test_hyde_failure_does_not_abort_recall(self):
        """If HyDE generation fails, recall still returns results (graceful degradation)."""
        from astrocyte.pipeline.orchestrator import PipelineOrchestrator as Orchestrator
        from astrocyte.types import RecallRequest

        vs = InMemoryVectorStore()
        llm = _FailingLLM()  # complete() always raises
        orch = Orchestrator(vector_store=vs, llm_provider=llm, enable_hyde=True)

        result = await orch.recall(RecallRequest(query="Where does Alice work?", bank_id="bank1"))
        assert result is not None
