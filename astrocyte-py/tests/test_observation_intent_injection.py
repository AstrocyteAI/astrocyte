"""Tests for intent-gated observation injection.

The ::obs bank is injected into RRF fusion only for EXPLORATORY and RELATIONAL
queries — the intents where synthesised observations add value over raw memories.
For FACTUAL / TEMPORAL / UNKNOWN intents the ::obs bank is skipped (default
observation_weight=0.0), protecting factual precision.

Covers:
- EXPLORATORY intent triggers injection
- RELATIONAL intent triggers injection
- FACTUAL intent skips injection
- TEMPORAL intent skips injection
- UNKNOWN intent skips injection
- observation_injection_weight default is 1.5
- explicit observation_weight > 0 still works for manual opt-in
- fact_types filter suppresses injection regardless of intent
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline.observation import obs_bank_id
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import RecallRequest, VectorItem

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orch(vs: InMemoryVectorStore, llm: MockLLMProvider) -> PipelineOrchestrator:
    return PipelineOrchestrator(
        vector_store=vs,
        llm_provider=llm,
        enable_observation_consolidation=True,
        observation_weight=0.0,           # global injection disabled
        observation_injection_weight=1.5,  # intent-gated weight
        enable_multi_query_expansion=False,
    )


async def _seed(vs: InMemoryVectorStore, llm: MockLLMProvider, bank: str) -> str:
    """Seed one raw memory and one observation; return the observation id."""
    raw_vec = llm._bow_embed("Alice plays guitar and paints watercolours.")
    obs_vec = llm._bow_embed("Alice is artistic and enjoys creative hobbies.")
    await vs.store_vectors([
        VectorItem(
            id="raw001",
            bank_id=bank,
            vector=raw_vec,
            text="Alice plays guitar and paints watercolours.",
        ),
        VectorItem(
            id="obs001",
            bank_id=obs_bank_id(bank),
            vector=obs_vec,
            text="Alice is artistic and enjoys creative hobbies.",
            fact_type="observation",
            metadata={"_obs_proof_count": 3},
        ),
    ])
    return "obs001"


# ---------------------------------------------------------------------------
# Intent-gated injection
# ---------------------------------------------------------------------------


class TestObservationIntentInjection:
    @pytest.mark.asyncio
    async def test_exploratory_intent_injects_observations(self):
        """EXPLORATORY query → ::obs bank injected into results."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        bank = "bank-exp"
        await _seed(vs, llm, bank)

        orch = _make_orch(vs, llm)
        # "tell me about" triggers EXPLORATORY
        result = await orch.recall(RecallRequest(query="tell me about Alice", bank_id=bank))

        hit_ids = {h.memory_id for h in result.hits}
        assert "obs001" in hit_ids

    @pytest.mark.asyncio
    async def test_relational_intent_injects_observations(self):
        """RELATIONAL query → ::obs bank injected."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        bank = "bank-rel"
        await _seed(vs, llm, bank)

        orch = _make_orch(vs, llm)
        # "relationship between" triggers RELATIONAL
        result = await orch.recall(
            RecallRequest(query="what is the relationship between Alice and her hobbies", bank_id=bank)
        )

        hit_ids = {h.memory_id for h in result.hits}
        assert "obs001" in hit_ids

    @pytest.mark.asyncio
    async def test_factual_intent_skips_observations(self):
        """FACTUAL query → ::obs bank NOT injected (default observation_weight=0.0)."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        bank = "bank-fact"
        await _seed(vs, llm, bank)

        orch = _make_orch(vs, llm)
        # "what" triggers FACTUAL
        result = await orch.recall(RecallRequest(query="what instrument does Alice play", bank_id=bank))

        hit_ids = {h.memory_id for h in result.hits}
        assert "obs001" not in hit_ids
        assert "raw001" in hit_ids

    @pytest.mark.asyncio
    async def test_temporal_intent_skips_observations(self):
        """TEMPORAL query → ::obs bank NOT injected."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        bank = "bank-temp"
        await _seed(vs, llm, bank)

        orch = _make_orch(vs, llm)
        result = await orch.recall(
            RecallRequest(query="when did Alice last play guitar", bank_id=bank)
        )

        hit_ids = {h.memory_id for h in result.hits}
        assert "obs001" not in hit_ids

    @pytest.mark.asyncio
    async def test_fact_types_filter_suppresses_injection(self):
        """fact_types filter always suppresses injection regardless of intent."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        bank = "bank-ft"
        await _seed(vs, llm, bank)

        orch = _make_orch(vs, llm)
        # EXPLORATORY intent but fact_types restricts to world memories
        result = await orch.recall(
            RecallRequest(
                query="tell me about Alice",
                bank_id=bank,
                fact_types=["world"],
            )
        )

        hit_ids = {h.memory_id for h in result.hits}
        assert "obs001" not in hit_ids


# ---------------------------------------------------------------------------
# Default parameter values
# ---------------------------------------------------------------------------


class TestObservationInjectionDefaults:
    def test_default_injection_weight_is_1_5(self):
        orch = PipelineOrchestrator(
            vector_store=InMemoryVectorStore(),
            llm_provider=MockLLMProvider(),
        )
        assert orch.observation_injection_weight == pytest.approx(1.5)

    def test_default_observation_weight_is_0(self):
        """Global observation_weight stays 0 — only intent gate enables injection."""
        orch = PipelineOrchestrator(
            vector_store=InMemoryVectorStore(),
            llm_provider=MockLLMProvider(),
        )
        assert orch.observation_weight == pytest.approx(0.0)

    def test_custom_injection_weight_stored(self):
        orch = PipelineOrchestrator(
            vector_store=InMemoryVectorStore(),
            llm_provider=MockLLMProvider(),
            observation_injection_weight=2.0,
        )
        assert orch.observation_injection_weight == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_explicit_observation_weight_still_works(self):
        """observation_weight > 0 injects for ALL intents (manual opt-in path)."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        bank = "bank-global"
        await _seed(vs, llm, bank)

        orch = PipelineOrchestrator(
            vector_store=vs,
            llm_provider=llm,
            enable_observation_consolidation=True,
            observation_weight=1.5,          # global injection enabled
            enable_multi_query_expansion=False,
        )
        # FACTUAL query — would normally skip injection
        result = await orch.recall(
            RecallRequest(query="what instrument does Alice play", bank_id=bank)
        )

        hit_ids = {h.memory_id for h in result.hits}
        assert "obs001" in hit_ids
