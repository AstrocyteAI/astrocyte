"""Tests for multi-query expansion confidence gate.

Covers:
- Gate suppresses decompose_query when top semantic score >= threshold
- Gate opens decompose_query when top semantic score < threshold
- Empty-bank guard always applies (fused=[]) regardless of threshold
- Threshold boundary values (0.0 = never expand, 1.0 = always expand when results exist)
- Default parameter values wired through correctly
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import Completion, Message, RecallRequest, TokenUsage, VectorItem

# ---------------------------------------------------------------------------
# Tracking LLM that intercepts decomposition calls
# ---------------------------------------------------------------------------


class _TrackingLLM(MockLLMProvider):
    """MockLLMProvider that counts decompose_query calls and controls the response.

    Detects decomposition calls by matching on ``"sub-questions"`` in the system
    prompt (from ``_DECOMPOSITION_SYSTEM`` in multi_query.py).  All other calls
    delegate to the standard MockLLMProvider implementation.
    """

    def __init__(self, sub_query_lines: str = "") -> None:
        super().__init__()
        self.decompose_calls: int = 0
        self._sub_query_lines = sub_query_lines

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Completion:
        system_content = next(
            (m.content for m in messages if m.role == "system" and isinstance(m.content, str)),
            "",
        )
        if "sub-questions" in system_content.lower():
            self.decompose_calls += 1
            return Completion(
                text=self._sub_query_lines,
                model=model or "mock",
                usage=TokenUsage(input_tokens=10, output_tokens=20),
            )
        return await super().complete(
            messages, model=model, max_tokens=max_tokens, temperature=temperature
        )


def _make_orch(
    vs: InMemoryVectorStore,
    llm: _TrackingLLM,
    *,
    threshold: float = 0.72,
) -> PipelineOrchestrator:
    return PipelineOrchestrator(
        vector_store=vs,
        llm_provider=llm,
        enable_multi_query_expansion=True,
        multi_query_confidence_threshold=threshold,
        # Disable observation consolidation so background tasks don't
        # interfere with LLM call counts.
        enable_observation_consolidation=False,
    )


# ---------------------------------------------------------------------------
# Confidence gate: high score suppresses expansion
# ---------------------------------------------------------------------------


class TestMultiQueryConfidenceGate:
    @pytest.mark.asyncio
    async def test_gate_skips_expansion_on_high_confidence(self):
        """Top semantic score >= threshold → decompose_query is not called."""
        vs = InMemoryVectorStore()
        llm = _TrackingLLM()

        # Pre-compute the BOW vector for the query text and store it directly.
        # Querying with the same text produces cosine_sim = 1.0 → well above 0.72.
        text = "Alice is a software engineer at Acme"
        stored_vec = llm._bow_embed(text)
        await vs.store_vectors([
            VectorItem(id="m1", bank_id="test-bank", vector=stored_vec, text=text),
        ])

        orch = _make_orch(vs, llm, threshold=0.72)
        await orch.recall(RecallRequest(query=text, bank_id="test-bank"))

        assert llm.decompose_calls == 0

    @pytest.mark.asyncio
    async def test_gate_triggers_expansion_on_low_confidence(self):
        """Top semantic score < threshold → decompose_query is called exactly once."""
        vs = InMemoryVectorStore()
        llm = _TrackingLLM(sub_query_lines="What is Alice's job?\nWhere does Alice work?")

        # Store content whose tokens share nothing with the query below.
        stored_vec = llm._bow_embed("oceanography marine biology coral reef tidal")
        await vs.store_vectors([
            VectorItem(
                id="m1",
                bank_id="test-bank",
                vector=stored_vec,
                text="Oceanography notes about coral reefs.",
            ),
        ])

        orch = _make_orch(vs, llm, threshold=0.72)
        # This query shares almost no tokens with the stored content → score ≈ 0.
        await orch.recall(RecallRequest(query="Alice software engineering career", bank_id="test-bank"))

        assert llm.decompose_calls == 1

    @pytest.mark.asyncio
    async def test_empty_bank_always_skips_expansion(self):
        """Empty bank → fused=[] → expansion is skipped regardless of threshold."""
        vs = InMemoryVectorStore()
        llm = _TrackingLLM()

        # threshold=0.0 would open the gate on any non-zero score, but fused=[]
        # means the outer guard triggers first.
        orch = _make_orch(vs, llm, threshold=0.0)
        await orch.recall(RecallRequest(query="anything", bank_id="empty-bank"))

        assert llm.decompose_calls == 0

    @pytest.mark.asyncio
    async def test_threshold_zero_disables_expansion(self):
        """threshold=0.0 → gate condition is score < 0.0, which is never true.

        Cosine similarity between non-negative BOW vectors is always >= 0.0,
        so no expansion fires even for low-relevance queries.
        """
        vs = InMemoryVectorStore()
        llm = _TrackingLLM()

        stored_vec = llm._bow_embed("Alice is an engineer")
        await vs.store_vectors([
            VectorItem(id="m1", bank_id="b", vector=stored_vec, text="Alice is an engineer"),
        ])

        orch = _make_orch(vs, llm, threshold=0.0)
        # Query overlaps with stored content → any positive score ≥ 0.0 = threshold
        await orch.recall(RecallRequest(query="Alice engineer", bank_id="b"))

        assert llm.decompose_calls == 0

    @pytest.mark.asyncio
    async def test_threshold_one_always_expands_when_results_exist(self):
        """threshold=1.0 → expand whenever cosine_sim < 1.0 (i.e. text is not identical)."""
        vs = InMemoryVectorStore()
        llm = _TrackingLLM(sub_query_lines="sub question one\nsub question two")

        # Stored text and query text differ → cosine sim < 1.0 → gate opens.
        stored_vec = llm._bow_embed("Alice designs distributed systems for Acme Corporation")
        await vs.store_vectors([
            VectorItem(id="m1", bank_id="b", vector=stored_vec, text="Alice designs systems"),
        ])

        orch = _make_orch(vs, llm, threshold=1.0)
        await orch.recall(RecallRequest(query="Alice software engineering career", bank_id="b"))

        assert llm.decompose_calls == 1

    @pytest.mark.asyncio
    async def test_simple_query_no_merge_when_llm_returns_original(self):
        """If decompose_query returns only the original query (already simple), no sub-query merge."""
        vs = InMemoryVectorStore()
        # LLM returns empty string → decompose_query returns [query] → len == 1 → no merge.
        llm = _TrackingLLM(sub_query_lines="")

        stored_vec = llm._bow_embed("oceanography tidal coral marine")
        await vs.store_vectors([
            VectorItem(id="m1", bank_id="b", vector=stored_vec, text="Coral reef notes."),
        ])

        orch = _make_orch(vs, llm, threshold=0.72)
        result = await orch.recall(RecallRequest(query="Alice career engineering", bank_id="b"))

        # Decomposition was called (low score) but no merge occurred.
        assert llm.decompose_calls == 1
        # Result should still contain the one stored item (from the original recall pass).
        assert len(result.hits) == 1


# ---------------------------------------------------------------------------
# Default parameter values
# ---------------------------------------------------------------------------


class TestMultiQueryDefaults:
    def test_expansion_enabled_by_default(self):
        orch = PipelineOrchestrator(
            vector_store=InMemoryVectorStore(),
            llm_provider=MockLLMProvider(),
        )
        assert orch.enable_multi_query_expansion is True

    def test_default_threshold_is_0_72(self):
        orch = PipelineOrchestrator(
            vector_store=InMemoryVectorStore(),
            llm_provider=MockLLMProvider(),
        )
        assert orch.multi_query_confidence_threshold == pytest.approx(0.72)

    def test_custom_threshold_is_stored(self):
        orch = PipelineOrchestrator(
            vector_store=InMemoryVectorStore(),
            llm_provider=MockLLMProvider(),
            multi_query_confidence_threshold=0.85,
        )
        assert orch.multi_query_confidence_threshold == pytest.approx(0.85)

    def test_expansion_can_be_disabled(self):
        orch = PipelineOrchestrator(
            vector_store=InMemoryVectorStore(),
            llm_provider=MockLLMProvider(),
            enable_multi_query_expansion=False,
        )
        assert orch.enable_multi_query_expansion is False
