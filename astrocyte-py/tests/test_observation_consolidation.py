"""Tests for the observation consolidation layer.

Covers:
- ObservationConsolidator.consolidate() — create / update / delete actions
- _parse_actions() — robust JSON extraction from LLM responses
- PipelineOrchestrator with enable_observation_consolidation=True
- _observation_proof_boost() in reranking
"""

from __future__ import annotations

import json

import pytest

from astrocyte.pipeline.fusion import ScoredItem
from astrocyte.pipeline.observation import (
    ObservationConsolidator,
    _parse_actions,
    obs_bank_id,
)
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.pipeline.reranking import _observation_proof_boost
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import RecallRequest, RetainRequest, VectorItem

# ---------------------------------------------------------------------------
# _parse_actions — unit tests (no I/O)
# ---------------------------------------------------------------------------


class TestParseActions:
    def test_create_action(self):
        raw = '[{"action": "create", "text": "Alice is an engineer.", "confidence": 0.9}]'
        actions = _parse_actions(raw)
        assert len(actions) == 1
        assert actions[0]["action"] == "create"
        assert actions[0]["text"] == "Alice is an engineer."
        assert actions[0]["confidence"] == 0.9

    def test_update_action(self):
        raw = '[{"action": "update", "obs_id": "abc123", "text": "Alice is a senior engineer.", "confidence": 0.95}]'
        actions = _parse_actions(raw)
        assert len(actions) == 1
        assert actions[0]["action"] == "update"
        assert actions[0]["obs_id"] == "abc123"

    def test_delete_action(self):
        raw = '[{"action": "delete", "obs_id": "xyz789"}]'
        actions = _parse_actions(raw)
        assert len(actions) == 1
        assert actions[0]["action"] == "delete"

    def test_empty_array(self):
        assert _parse_actions("[]") == []

    def test_leading_prose_stripped(self):
        raw = 'Here are the actions:\n[{"action": "create", "text": "Bob likes hiking.", "confidence": 0.8}]'
        actions = _parse_actions(raw)
        assert len(actions) == 1

    def test_markdown_code_fence_stripped(self):
        raw = '```json\n[{"action": "create", "text": "Carol is a doctor.", "confidence": 0.7}]\n```'
        actions = _parse_actions(raw)
        assert len(actions) == 1

    def test_invalid_json_returns_empty(self):
        assert _parse_actions("not json at all") == []

    def test_invalid_action_type_filtered(self):
        raw = '[{"action": "explode", "text": "boom"}, {"action": "create", "text": "ok", "confidence": 0.9}]'
        actions = _parse_actions(raw)
        assert len(actions) == 1
        assert actions[0]["action"] == "create"

    def test_mixed_valid_and_invalid(self):
        raw = '[{"action": "create", "text": "A", "confidence": 0.9}, "not_a_dict", {"action": "delete", "obs_id": "x"}]'
        actions = _parse_actions(raw)
        assert len(actions) == 2


# ---------------------------------------------------------------------------
# _observation_proof_boost — unit tests
# ---------------------------------------------------------------------------


class TestObservationProofBoost:
    def _make_item(self, fact_type: str | None, proof_count: int | None) -> ScoredItem:
        meta = {}
        if proof_count is not None:
            meta["_obs_proof_count"] = proof_count
        return ScoredItem(
            id="x",
            text="test",
            score=0.5,
            fact_type=fact_type,
            metadata=meta if meta else None,
        )

    def test_raw_memory_no_boost(self):
        item = self._make_item(fact_type=None, proof_count=None)
        assert _observation_proof_boost(item) == 0.0

    def test_observation_single_proof_no_boost(self):
        item = self._make_item(fact_type="observation", proof_count=1)
        assert _observation_proof_boost(item) == 0.0

    def test_observation_two_proofs_small_boost(self):
        item = self._make_item(fact_type="observation", proof_count=2)
        boost = _observation_proof_boost(item)
        assert boost > 0.0
        assert boost == pytest.approx(0.025)

    def test_observation_proof_count_capped(self):
        item = self._make_item(fact_type="observation", proof_count=100)
        boost = _observation_proof_boost(item)
        # Cap at OBSERVATION_PROOF_CAP (4) × OBSERVATION_PROOF_WEIGHT (0.025)
        assert boost == pytest.approx(0.1)

    def test_non_observation_fact_type_no_boost(self):
        item = self._make_item(fact_type="world", proof_count=5)
        assert _observation_proof_boost(item) == 0.0


# ---------------------------------------------------------------------------
# ObservationConsolidator — integration tests with in-memory providers
# ---------------------------------------------------------------------------


def _make_json_llm(json_response: str) -> MockLLMProvider:
    """Return a MockLLMProvider whose complete() always returns json_response."""
    return MockLLMProvider(default_response=json_response)


class TestObservationConsolidatorCreate:
    @pytest.mark.asyncio
    async def test_create_stores_observation(self):
        vs = InMemoryVectorStore()
        llm = _make_json_llm('[{"action": "create", "text": "Alice is a software engineer.", "confidence": 0.9}]')
        consolidator = ObservationConsolidator()

        result = await consolidator.consolidate(
            new_memory_text="Alice told me she works as a software engineer.",
            new_memory_ids=["mem001"],
            bank_id="bank-a",
            vector_store=vs,
            llm_provider=llm,
        )

        assert result.created == 1
        assert result.errors == []

        # Verify the observation was stored in the dedicated obs bank
        items = await vs.list_vectors(obs_bank_id("bank-a"))
        obs = [i for i in items if i.fact_type == "observation"]
        assert len(obs) == 1
        # Raw memory bank should be untouched by consolidation
        assert await vs.list_vectors("bank-a") == []
        assert obs[0].text == "Alice is a software engineer."
        assert obs[0].metadata is not None
        assert obs[0].metadata["_obs_proof_count"] == 1
        assert json.loads(str(obs[0].metadata["_obs_source_ids"])) == ["mem001"]
        assert float(str(obs[0].metadata["_obs_confidence"])) == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_low_confidence_observation_not_stored(self):
        vs = InMemoryVectorStore()
        llm = _make_json_llm('[{"action": "create", "text": "Maybe Alice is a pilot.", "confidence": 0.3}]')
        consolidator = ObservationConsolidator(min_confidence=0.5)

        result = await consolidator.consolidate(
            new_memory_text="Alice might be a pilot according to someone.",
            new_memory_ids=["mem002"],
            bank_id="bank-b",
            vector_store=vs,
            llm_provider=llm,
        )

        assert result.created == 0
        assert result.skipped == 1
        items = await vs.list_vectors(obs_bank_id("bank-b"))
        assert items == []

    @pytest.mark.asyncio
    async def test_empty_actions_noop(self):
        vs = InMemoryVectorStore()
        llm = _make_json_llm("[]")
        consolidator = ObservationConsolidator()

        result = await consolidator.consolidate(
            new_memory_text="Redundant memory.",
            new_memory_ids=["mem003"],
            bank_id="bank-c",
            vector_store=vs,
            llm_provider=llm,
        )

        assert result.created == 0
        assert result.updated == 0
        assert result.deleted == 0
        assert await vs.list_vectors(obs_bank_id("bank-c")) == []


class TestObservationConsolidatorUpdate:
    @pytest.mark.asyncio
    async def test_update_increments_proof_count(self):
        """Update action should delete old obs and store revised version with proof_count+1."""
        vs = InMemoryVectorStore()
        llm_create = _make_json_llm('[{"action": "create", "text": "Alice is an engineer.", "confidence": 0.8}]')
        consolidator = ObservationConsolidator()

        # First retain → creates observation
        await consolidator.consolidate(
            new_memory_text="Alice works as an engineer.",
            new_memory_ids=["mem001"],
            bank_id="bank-upd",
            vector_store=vs,
            llm_provider=llm_create,
        )

        items = await vs.list_vectors(obs_bank_id("bank-upd"))
        obs = [i for i in items if i.fact_type == "observation"]
        assert len(obs) == 1
        obs_id = obs[0].id

        # Second retain → update action referencing the existing obs
        llm_update = _make_json_llm(
            f'[{{"action": "update", "obs_id": "{obs_id}", '
            f'"text": "Alice is a senior engineer.", "confidence": 0.92}}]'
        )
        result = await consolidator.consolidate(
            new_memory_text="Alice got promoted to senior engineer.",
            new_memory_ids=["mem002"],
            bank_id="bank-upd",
            vector_store=vs,
            llm_provider=llm_update,
        )

        assert result.updated == 1
        assert result.errors == []

        items = await vs.list_vectors(obs_bank_id("bank-upd"))
        obs_after = [i for i in items if i.fact_type == "observation"]
        assert len(obs_after) == 1  # old one deleted, new one added
        updated = obs_after[0]
        assert updated.text == "Alice is a senior engineer."
        assert updated.metadata is not None
        assert updated.metadata["_obs_proof_count"] == 2  # incremented from 1
        source_ids = json.loads(str(updated.metadata["_obs_source_ids"]))
        assert "mem001" in source_ids
        assert "mem002" in source_ids


class TestObservationConsolidatorDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_observation(self):
        vs = InMemoryVectorStore()
        llm_create = _make_json_llm('[{"action": "create", "text": "Alice likes coffee.", "confidence": 0.7}]')
        consolidator = ObservationConsolidator()

        await consolidator.consolidate(
            new_memory_text="Alice drinks coffee every morning.",
            new_memory_ids=["mem001"],
            bank_id="bank-del",
            vector_store=vs,
            llm_provider=llm_create,
        )
        obs = [i for i in await vs.list_vectors(obs_bank_id("bank-del")) if i.fact_type == "observation"]
        assert len(obs) == 1
        obs_id = obs[0].id

        llm_delete = _make_json_llm(f'[{{"action": "delete", "obs_id": "{obs_id}"}}]')
        result = await consolidator.consolidate(
            new_memory_text="Alice has stopped drinking coffee.",
            new_memory_ids=["mem002"],
            bank_id="bank-del",
            vector_store=vs,
            llm_provider=llm_delete,
        )

        assert result.deleted == 1
        obs_after = [i for i in await vs.list_vectors(obs_bank_id("bank-del")) if i.fact_type == "observation"]
        assert len(obs_after) == 0


# ---------------------------------------------------------------------------
# PipelineOrchestrator — enable_observation_consolidation=True
# ---------------------------------------------------------------------------


class TestOrchestratorObservationIntegration:
    @pytest.mark.asyncio
    async def test_orchestrator_observation_flag_accepted(self):
        """Constructing an orchestrator with observation consolidation enabled should not raise."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(
            vector_store=vs,
            llm_provider=llm,
            enable_observation_consolidation=True,
        )
        assert orch.enable_observation_consolidation is True
        assert orch._observation_consolidator is not None

    @pytest.mark.asyncio
    async def test_orchestrator_observation_enabled_by_default(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vector_store=vs, llm_provider=llm)
        assert orch.enable_observation_consolidation is True
        assert orch._observation_consolidator is not None

    @pytest.mark.asyncio
    async def test_orchestrator_observation_can_be_disabled(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vector_store=vs, llm_provider=llm, enable_observation_consolidation=False)
        assert orch.enable_observation_consolidation is False
        assert orch._observation_consolidator is None

    @pytest.mark.asyncio
    async def test_retain_does_not_raise_with_observation_enabled(self):
        """retain() with observation consolidation enabled should complete normally.

        The fire-and-forget consolidation task runs after the response is
        returned.  We run the event loop briefly to flush pending tasks.
        """
        import asyncio

        vs = InMemoryVectorStore()
        # Return JSON for consolidation; MockLLMProvider default covers other prompts
        llm = MockLLMProvider(
            default_response='[{"action": "create", "text": "Bob is a designer.", "confidence": 0.85}]'
        )
        orch = PipelineOrchestrator(
            vector_store=vs,
            llm_provider=llm,
            enable_observation_consolidation=True,
        )

        result = await orch.retain(
            RetainRequest(
                content="Bob mentioned he works as a designer.",
                bank_id="bank-orch",
            )
        )
        assert result.stored is True

        # Flush the background consolidation task
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_recall_obs_bank_separate_from_raw_bank(self):
        """Observations live in the ::obs bank; main recall bank is never polluted.

        With observation_weight=0.0 (default), the observation strategy is disabled
        in recall — the ::obs bank is written during retain but not injected into RRF.
        This prevents abstract observation summaries from displacing verbatim raw
        memories in the top-k, which was observed to halve precision in benchmarks.
        Observations can be re-enabled for specific query intents via observation_weight.
        """
        from astrocyte.pipeline.observation import obs_bank_id

        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(
            vector_store=vs,
            llm_provider=llm,
            enable_observation_consolidation=True,
            # observation_weight=0.0 by default — injection disabled
        )
        # Raw memory in the main bank
        await vs.store_vectors([
            VectorItem(
                id="raw001",
                bank_id="bank-filter",
                vector=[1.0] + [0.0] * 127,
                text="Charlie plays guitar.",
                fact_type="world",
            ),
        ])
        # Observation in the separate ::obs bank (written by consolidation, not by retain)
        await vs.store_vectors([
            VectorItem(
                id="obs001",
                bank_id=obs_bank_id("bank-filter"),
                vector=[1.0] + [0.0] * 127,
                text="Charlie is a musician.",
                fact_type="observation",
                metadata={"_obs_proof_count": 2},
            ),
        ])
        # Normal recall: only raw bank is searched (observation_weight=0.0 → no injection)
        result = await orch.recall(
            RecallRequest(query="Charlie", bank_id="bank-filter", max_results=10)
        )
        hit_ids = {h.memory_id for h in result.hits}
        assert "raw001" in hit_ids
        assert "obs001" not in hit_ids  # ::obs bank not injected by default

        # Opt-in: explicit observation_weight > 0 enables injection
        orch_with_obs = PipelineOrchestrator(
            vector_store=vs,
            llm_provider=llm,
            enable_observation_consolidation=True,
            observation_weight=1.5,
        )
        result_with_obs = await orch_with_obs.recall(
            RecallRequest(query="Charlie", bank_id="bank-filter", max_results=10)
        )
        obs_hit_ids = {h.memory_id for h in result_with_obs.hits}
        assert "obs001" in obs_hit_ids  # ::obs bank injected at 1.5× weight
