"""Tests for the entity co-occurrence link cap (2026-05-06 retain
profile fix).

The orchestrator builds ``co_occurs`` ``EntityLink`` rows between
entities mentioned in the same memory. Without a cap this is an
all-pairs Cartesian product (``C(N,2)`` per retain) — the LME retain
profile measured this as 34% of retain wall with O(N²) cost growth as
the entity-link table grew. Capping at K=5 entities per memory bounds
the work to ``C(K,2)=10`` pairs and removes the drift.

Two layers of tests:

1. ``_build_cooccurrence_pairs`` (pure helper) — exhaustive cap
   semantics. Cap respected, head-of-list selection, edge cases.
2. ``EntityCooccurrenceConfig`` → pipeline attribute propagation —
   ensures the config knob actually reaches the runtime checker.
"""

from __future__ import annotations

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.pipeline.orchestrator import (
    PipelineOrchestrator,
    _build_cooccurrence_pairs,
)
from astrocyte.testing.in_memory import (
    InMemoryGraphStore,
    InMemoryVectorStore,
    MockLLMProvider,
)


class TestBuildCooccurrencePairs:
    """Pure-function cap semantics. Encodes all the behaviours that
    matter for retain throughput:

    - Empty / single-entity inputs emit no pairs (caller skips storage).
    - When N ≤ K, all C(N,2) pairs returned (no truncation).
    - When N > K, only the first K entities form pairs (head-of-list
      preserves extraction order).
    - K = 1 is clamped to 2 (the minimum that produces any pairs at
      all) — guards against config typos that would silently disable
      co-occurrence.
    """

    def test_empty_input_returns_empty(self):
        assert _build_cooccurrence_pairs([], max_entities=5) == []

    def test_single_entity_returns_empty(self):
        assert _build_cooccurrence_pairs(["e1"], max_entities=5) == []

    def test_two_entities_under_cap(self):
        # N=2, K=5 → C(2,2) = 1 pair
        assert _build_cooccurrence_pairs(["e1", "e2"], max_entities=5) == [
            ("e1", "e2"),
        ]

    def test_n_equal_k_full_cartesian(self):
        # N=K=3 → C(3,2) = 3 pairs, no truncation
        pairs = _build_cooccurrence_pairs(["e1", "e2", "e3"], max_entities=3)
        assert sorted(pairs) == [("e1", "e2"), ("e1", "e3"), ("e2", "e3")]

    def test_n_greater_than_k_caps(self):
        # N=10, K=5 → only first 5 entities form pairs → C(5,2) = 10
        ids = [f"e{i}" for i in range(10)]
        pairs = _build_cooccurrence_pairs(ids, max_entities=5)
        assert len(pairs) == 10
        # Every pair member must come from the first K = the cap is
        # head-of-list, not random sample.
        head = set(ids[:5])
        for a, b in pairs:
            assert a in head and b in head, (
                f"pair ({a}, {b}) used an entity outside the head — cap is not preserving extraction order"
            )

    def test_dense_session_bounded(self):
        # The exact LME-shaped case: 30 entities → 10 pairs (vs 435
        # without the cap). The 43× reduction is the whole point of
        # the fix.
        ids = [f"e{i}" for i in range(30)]
        pairs = _build_cooccurrence_pairs(ids, max_entities=5)
        assert len(pairs) == 10

    def test_k_below_minimum_clamped(self):
        # K=1 would emit no pairs (you need ≥2 entities to pair) —
        # clamp to 2 so a config typo doesn't silently disable
        # co-occurrence.
        pairs = _build_cooccurrence_pairs(["e1", "e2", "e3"], max_entities=1)
        # K=1 clamps to K=2 → only first 2 entities pair → 1 link
        assert pairs == [("e1", "e2")]


class TestEntityCooccurrenceConfigWiring:
    """``EntityCooccurrenceConfig`` is set by ``Astrocyte.set_pipeline``
    onto the pipeline's runtime attributes. Pin so the wiring isn't
    silently dropped — the cap is useless if the config never reaches
    the checker."""

    def _make_pipeline(self, config: AstrocyteConfig) -> PipelineOrchestrator:
        brain = Astrocyte(config)
        pipeline = PipelineOrchestrator(
            vector_store=InMemoryVectorStore(),
            graph_store=InMemoryGraphStore(),
            llm_provider=MockLLMProvider(),
        )
        brain.set_pipeline(pipeline)
        return pipeline

    def test_default_propagation(self):
        config = AstrocyteConfig()
        pipeline = self._make_pipeline(config)
        # Defaults: capped behaviour, K=5.
        assert pipeline.entity_cooccurrence_enabled is True
        assert pipeline.entity_cooccurrence_max_entities == 5

    def test_custom_max_propagates(self):
        config = AstrocyteConfig()
        config.entity_cooccurrence.max_entities_per_memory = 8
        pipeline = self._make_pipeline(config)
        assert pipeline.entity_cooccurrence_max_entities == 8

    def test_disable_propagates(self):
        config = AstrocyteConfig()
        config.entity_cooccurrence.enabled = False
        pipeline = self._make_pipeline(config)
        assert pipeline.entity_cooccurrence_enabled is False
