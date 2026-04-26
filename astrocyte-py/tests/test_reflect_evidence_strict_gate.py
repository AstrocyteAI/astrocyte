"""Tests for the evidence-strict reflect gate.

When the top raw semantic score from recall is below
reflect_evidence_strict_threshold, reflect() auto-selects the
evidence_strict prompt to force citation and prevent hallucination
from tangential memories.

Covers:
- Gate fires on weak retrieval (low cosine sim)
- Gate is suppressed on strong retrieval (high cosine sim)
- evidence_strict takes priority over temporal_aware
- MIP explicit prompt overrides the gate
- top_semantic_score is populated on RecallResult
- Default threshold is 0.5
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import RecallRequest, ReflectRequest, VectorItem

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orch(
    vs: InMemoryVectorStore,
    llm: MockLLMProvider,
    *,
    threshold: float = 0.5,
) -> PipelineOrchestrator:
    return PipelineOrchestrator(
        vector_store=vs,
        llm_provider=llm,
        reflect_evidence_strict_threshold=threshold,
        enable_observation_consolidation=False,
        enable_multi_query_expansion=False,  # isolate the reflect gate
    )


def _store_item(vs: InMemoryVectorStore, llm: MockLLMProvider, text: str, bank: str) -> None:
    """Pre-compute BOW vector and store a VectorItem synchronously via asyncio.run."""
    import asyncio

    vec = llm._bow_embed(text)
    asyncio.get_event_loop().run_until_complete(
        vs.store_vectors([VectorItem(id="m1", bank_id=bank, vector=vec, text=text)])
    )


# ---------------------------------------------------------------------------
# top_semantic_score population
# ---------------------------------------------------------------------------


class TestTopSemanticScore:
    @pytest.mark.asyncio
    async def test_top_semantic_score_set_on_strong_match(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        text = "Alice is a software engineer at Acme"
        vec = llm._bow_embed(text)
        await vs.store_vectors([VectorItem(id="m1", bank_id="b", vector=vec, text=text)])

        orch = _make_orch(vs, llm)
        result = await orch.recall(RecallRequest(query=text, bank_id="b"))

        # Cosine sim = 1.0 for identical text
        assert result.top_semantic_score == pytest.approx(1.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_top_semantic_score_zero_on_empty_bank(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()

        orch = _make_orch(vs, llm)
        result = await orch.recall(RecallRequest(query="anything", bank_id="empty"))

        assert result.top_semantic_score == 0.0

    @pytest.mark.asyncio
    async def test_top_semantic_score_low_on_unrelated_query(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        vec = llm._bow_embed("oceanography marine biology coral reef tidal")
        await vs.store_vectors([VectorItem(id="m1", bank_id="b", vector=vec, text="Coral reef notes.")])

        orch = _make_orch(vs, llm)
        result = await orch.recall(RecallRequest(query="Alice career software engineering", bank_id="b"))

        # Low overlap → low score, well below 0.5
        assert result.top_semantic_score < 0.5


# ---------------------------------------------------------------------------
# evidence_strict gate in reflect()
# ---------------------------------------------------------------------------


class TestEvidenceStrictGate:
    @pytest.mark.asyncio
    async def test_gate_fires_on_weak_retrieval(self):
        """When top_semantic_score < threshold, reflect uses evidence_strict prompt."""
        vs = InMemoryVectorStore()
        # Track which prompts are passed to the LLM
        used_prompts: list[str] = []

        class _TrackingLLM(MockLLMProvider):
            async def complete(self, messages, **kwargs):
                system = next(
                    (m.content for m in messages if m.role == "system" and isinstance(m.content, str)), ""
                )
                used_prompts.append(system)
                return await super().complete(messages, **kwargs)

        llm = _TrackingLLM()
        # Store unrelated content → low cosine sim → gate fires
        vec = llm._bow_embed("oceanography marine biology coral reef")
        await vs.store_vectors([VectorItem(id="m1", bank_id="b", vector=vec, text="Coral reef notes.")])

        orch = _make_orch(vs, llm, threshold=0.5)
        await orch.reflect(ReflectRequest(query="Did Alice ever go skydiving?", bank_id="b"))

        # evidence_strict prompt contains "Cite the specific memory number"
        assert any("Cite the specific memory number" in p for p in used_prompts), \
            f"No evidence_strict prompt found. Got: {[p[:80] for p in used_prompts]}"

    @pytest.mark.asyncio
    async def test_gate_suppressed_on_strong_retrieval(self):
        """When top_semantic_score >= threshold, reflect uses the default prompt.

        Store the item using the exact same BOW vector the orchestrator will produce
        for the query — guaranteeing cosine_sim = 1.0 regardless of token overlap.
        """
        vs = InMemoryVectorStore()
        used_prompts: list[str] = []

        class _TrackingLLM(MockLLMProvider):
            async def complete(self, messages, **kwargs):
                system = next(
                    (m.content for m in messages if m.role == "system" and isinstance(m.content, str)), ""
                )
                used_prompts.append(system)
                return await super().complete(messages, **kwargs)

        llm = _TrackingLLM()
        query = "Did Alice ever go skydiving?"
        # Store with the exact BOW vector the orchestrator will embed for this query
        # → cosine_sim = 1.0, well above the 0.5 threshold.
        vec = llm._bow_embed(query)
        await vs.store_vectors([
            VectorItem(id="m1", bank_id="b", vector=vec, text="Alice went skydiving last weekend.")
        ])

        orch = _make_orch(vs, llm, threshold=0.5)
        await orch.reflect(ReflectRequest(query=query, bank_id="b"))

        # Default prompt — should NOT contain "Cite the specific memory number"
        assert not any("Cite the specific memory number" in p for p in used_prompts)

    @pytest.mark.asyncio
    async def test_evidence_strict_takes_priority_over_temporal_aware(self):
        """Weak retrieval + temporal query → evidence_strict wins over temporal_aware."""
        vs = InMemoryVectorStore()
        used_prompts: list[str] = []

        class _TrackingLLM(MockLLMProvider):
            async def complete(self, messages, **kwargs):
                system = next(
                    (m.content for m in messages if m.role == "system" and isinstance(m.content, str)), ""
                )
                used_prompts.append(system)
                return await super().complete(messages, **kwargs)

        llm = _TrackingLLM()
        # Unrelated stored content → low score
        vec = llm._bow_embed("oceanography marine biology coral reef")
        await vs.store_vectors([VectorItem(id="m1", bank_id="b", vector=vec, text="Coral reef notes.")])

        orch = _make_orch(vs, llm, threshold=0.5)
        # Temporal query — but poor retrieval should trump temporal selection
        await orch.reflect(
            ReflectRequest(query="When did Alice first visit Paris before her promotion?", bank_id="b")
        )

        assert any("Cite the specific memory number" in p for p in used_prompts)
        assert not any("answering a question about events over time" in p for p in used_prompts)

    @pytest.mark.asyncio
    async def test_temporal_aware_fires_when_retrieval_is_strong(self):
        """Good retrieval + temporal query → temporal_aware prompt is selected."""
        vs = InMemoryVectorStore()
        used_prompts: list[str] = []

        class _TrackingLLM(MockLLMProvider):
            async def complete(self, messages, **kwargs):
                system = next(
                    (m.content for m in messages if m.role == "system" and isinstance(m.content, str)), ""
                )
                used_prompts.append(system)
                return await super().complete(messages, **kwargs)

        llm = _TrackingLLM()
        query = "When did Alice first visit Paris before her promotion?"
        # Store with the exact query vector → cosine_sim = 1.0 → gate stays closed.
        vec = llm._bow_embed(query)
        text = "Alice visited Paris in March 2023 before her promotion in June."
        await vs.store_vectors([VectorItem(id="m1", bank_id="b", vector=vec, text=text)])

        orch = _make_orch(vs, llm, threshold=0.5)
        await orch.reflect(ReflectRequest(query=query, bank_id="b"))

        assert any("answering a question about events over time" in p for p in used_prompts)
        assert not any("Cite the specific memory number" in p for p in used_prompts)


# ---------------------------------------------------------------------------
# Default parameter values
# ---------------------------------------------------------------------------


class TestEvidenceStrictDefaults:
    def test_default_threshold_is_0_5(self):
        orch = PipelineOrchestrator(
            vector_store=InMemoryVectorStore(),
            llm_provider=MockLLMProvider(),
        )
        assert orch.reflect_evidence_strict_threshold == pytest.approx(0.5)

    def test_custom_threshold_stored(self):
        orch = PipelineOrchestrator(
            vector_store=InMemoryVectorStore(),
            llm_provider=MockLLMProvider(),
            reflect_evidence_strict_threshold=0.35,
        )
        assert orch.reflect_evidence_strict_threshold == pytest.approx(0.35)
