"""Tests for `astrocyte.pipeline.fact_recall.fact_recall`.

Verifies (RRF-fused architecture):
  - Semantic search always runs
  - Episodic search runs only when (a) config flag on AND (b) question has cue
  - Temporal search runs only when caller passes ``temporal_range``
  - Failures in any branch are isolated (other branches still produce)
  - Dedupe by fact_id with RRF score accumulation
  - RRF ordering: cross-strategy agreement ranks above single-strategy hits
"""

from __future__ import annotations

from datetime import datetime

import pytest

from astrocyte.config import AstrocyteConfig, EpisodicExtractConfig
from astrocyte.pipeline.fact_recall import fact_recall


def _cfg(episodic: bool = False) -> AstrocyteConfig:
    c = AstrocyteConfig()
    c.episodic_extract = EpisodicExtractConfig(enabled=episodic)
    return c


class _FactHit:
    """Minimal stand-in for a PageIndexFact hit."""

    def __init__(self, fact_id: str, text: str = ""):
        self.fact_id = fact_id
        self.text = text


class _FakeStore:
    def __init__(
        self,
        semantic_hits=None,
        episodic_hits=None,
        temporal_hits=None,
        fail_semantic: bool = False,
        fail_episodic: bool = False,
        fail_temporal: bool = False,
    ):
        self._semantic_hits = semantic_hits or []
        self._episodic_hits = episodic_hits or []
        self._temporal_hits = temporal_hits or []
        self._fail_semantic = fail_semantic
        self._fail_episodic = fail_episodic
        self._fail_temporal = fail_temporal
        self.semantic_calls = 0
        self.episodic_calls = 0
        self.temporal_calls = 0

    async def search_facts_semantic(self, bank_id, qvec, *, top_k, document_id):
        self.semantic_calls += 1
        if self._fail_semantic:
            raise RuntimeError("semantic broke")
        return self._semantic_hits

    async def search_facts_by_entity(self, bank_id, entity, *, top_k, document_id):
        self.episodic_calls += 1
        if self._fail_episodic:
            raise RuntimeError("episodic broke")
        return self._episodic_hits

    async def search_facts_temporal(self, bank_id, date_range, *, top_k, document_id):
        self.temporal_calls += 1
        if self._fail_temporal:
            raise RuntimeError("temporal broke")
        return self._temporal_hits


# ────────────────────────────────────────────────────────────────────────
# Branch gating
# ────────────────────────────────────────────────────────────────────────


class TestSemanticAlwaysRuns:
    @pytest.mark.asyncio
    async def test_returns_semantic_when_all_other_branches_off(self) -> None:
        store = _FakeStore(semantic_hits=[_FactHit("f1"), _FactHit("f2")])
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="who attended the wedding?",
            query_embedding=[0.1, 0.2], config=_cfg(episodic=False),
        )
        assert [h.fact_id for h in out] == ["f1", "f2"]
        assert store.semantic_calls == 1
        assert store.episodic_calls == 0
        assert store.temporal_calls == 0


class TestEpisodicGating:
    @pytest.mark.asyncio
    async def test_episodic_runs_when_flag_on_and_cue_present(self) -> None:
        store = _FakeStore(
            semantic_hits=[_FactHit("f1")],
            episodic_hits=[_FactHit("e1"), _FactHit("e2")],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="where did I meet Alice?",
            query_embedding=[0.1], config=_cfg(episodic=True),
        )
        ids = [h.fact_id for h in out]
        assert "f1" in ids
        assert "e1" in ids and "e2" in ids
        assert store.episodic_calls == 1

    @pytest.mark.asyncio
    async def test_episodic_skipped_when_no_cue(self) -> None:
        store = _FakeStore(semantic_hits=[_FactHit("f1")])
        await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="what is my favourite color?",
            query_embedding=[0.1], config=_cfg(episodic=True),
        )
        assert store.semantic_calls == 1
        assert store.episodic_calls == 0


class TestTemporalGating:
    @pytest.mark.asyncio
    async def test_temporal_runs_when_range_provided(self) -> None:
        store = _FakeStore(
            semantic_hits=[_FactHit("f1")],
            temporal_hits=[_FactHit("t1"), _FactHit("t2")],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="what happened on May 5th?", query_embedding=[0.1],
            config=_cfg(episodic=False),
            temporal_range=(datetime(2024, 5, 5), datetime(2024, 5, 5, 23, 59)),
        )
        ids = [h.fact_id for h in out]
        assert "f1" in ids
        assert "t1" in ids and "t2" in ids
        assert store.temporal_calls == 1

    @pytest.mark.asyncio
    async def test_temporal_skipped_when_range_none(self) -> None:
        store = _FakeStore(semantic_hits=[_FactHit("f1")])
        await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="anything", query_embedding=[0.1],
            config=_cfg(episodic=False),
            temporal_range=None,
        )
        assert store.temporal_calls == 0


# ────────────────────────────────────────────────────────────────────────
# RRF fusion behavior
# ────────────────────────────────────────────────────────────────────────


class TestRRFFusion:
    @pytest.mark.asyncio
    async def test_dedupe_by_fact_id(self) -> None:
        """A fact returned by multiple branches appears once in the output."""
        shared = _FactHit("shared-id")
        store = _FakeStore(
            semantic_hits=[shared, _FactHit("f1")],
            episodic_hits=[shared, _FactHit("e1")],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="where did I meet Bob?", query_embedding=[0.1],
            config=_cfg(episodic=True),
        )
        ids = [h.fact_id for h in out]
        assert ids.count("shared-id") == 1
        assert set(ids) == {"shared-id", "f1", "e1"}

    @pytest.mark.asyncio
    async def test_cross_strategy_agreement_ranks_above_single_strategy(self) -> None:
        """A fact that appears in BOTH semantic and temporal must outrank
        a fact that only appears in semantic at the same rank. This is
        the core RRF property that makes the fusion robust to false-
        positive temporal hits."""
        # 'shared' is rank-0 in both semantic and temporal
        # 'only_semantic' is rank-0 in semantic only
        shared = _FactHit("shared")
        only_semantic = _FactHit("only_semantic")
        only_temporal = _FactHit("only_temporal")
        store = _FakeStore(
            semantic_hits=[shared, only_semantic],
            temporal_hits=[shared, only_temporal],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="anything", query_embedding=[0.1],
            config=_cfg(episodic=False),
            temporal_range=(datetime(2024, 5, 5), datetime(2024, 5, 6)),
        )
        ids = [h.fact_id for h in out]
        # 'shared' must be first (it scores 1/61 + 1/61 = ~0.0328)
        # 'only_semantic' and 'only_temporal' each score 1/61 = ~0.0164
        assert ids[0] == "shared"
        assert set(ids[1:]) == {"only_semantic", "only_temporal"}

    @pytest.mark.asyncio
    async def test_higher_rank_in_one_strategy_beats_lower_rank_in_two(self) -> None:
        """RRF k=60 means very close differences in ranks matter less
        than appearing in multiple strategies, but a top-1 in semantic
        still beats a rank-5 + rank-5 pairing.

        rank-0 in semantic alone: 1/61 ≈ 0.01639
        rank-4 + rank-4 in semantic + temporal: 1/65 + 1/65 ≈ 0.03077
        → multi-strategy wins here
        """
        # Test the boundary: a fact at very low rank in two strategies vs
        # rank-0 in one
        top_in_sem = _FactHit("top_in_sem")
        mid_in_both = _FactHit("mid_in_both")
        store = _FakeStore(
            semantic_hits=[top_in_sem] + [_FactHit(f"pad_s{i}") for i in range(4)] + [mid_in_both],
            temporal_hits=[_FactHit(f"pad_t{i}") for i in range(4)] + [mid_in_both],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="anything", query_embedding=[0.1],
            config=_cfg(episodic=False),
            temporal_range=(datetime(2024, 5, 5), datetime(2024, 5, 6)),
        )
        ids = [h.fact_id for h in out]
        # mid_in_both: 1/65 + 1/65 ≈ 0.03077
        # top_in_sem: 1/61 ≈ 0.01639
        # → mid_in_both should outrank top_in_sem
        assert ids.index("mid_in_both") < ids.index("top_in_sem")

    @pytest.mark.asyncio
    async def test_hits_without_fact_id_are_dropped(self) -> None:
        """A hit object missing ``fact_id`` can't be deduped safely;
        the fuser drops it rather than risk inflating the candidate pool."""
        good = _FactHit("good")
        bad = _FactHit("bad")
        bad.fact_id = None  # type: ignore[assignment]
        store = _FakeStore(semantic_hits=[good, bad])
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="anything", query_embedding=[0.1],
            config=_cfg(episodic=False),
        )
        assert [h.fact_id for h in out] == ["good"]


# ────────────────────────────────────────────────────────────────────────
# Failure isolation
# ────────────────────────────────────────────────────────────────────────


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_semantic_failure_returns_other_branches(self) -> None:
        store = _FakeStore(
            episodic_hits=[_FactHit("e1")],
            temporal_hits=[_FactHit("t1")],
            fail_semantic=True,
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="where did I meet?", query_embedding=[0.1],
            config=_cfg(episodic=True),
            temporal_range=(datetime(2024, 5, 5), datetime(2024, 5, 6)),
        )
        ids = [h.fact_id for h in out]
        assert set(ids) == {"e1", "t1"}

    @pytest.mark.asyncio
    async def test_episodic_failure_returns_semantic(self) -> None:
        store = _FakeStore(
            semantic_hits=[_FactHit("f1")], fail_episodic=True,
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="where did I meet?", query_embedding=[0.1],
            config=_cfg(episodic=True),
        )
        assert [h.fact_id for h in out] == ["f1"]

    @pytest.mark.asyncio
    async def test_temporal_failure_returns_semantic(self) -> None:
        store = _FakeStore(
            semantic_hits=[_FactHit("f1")], fail_temporal=True,
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="anything", query_embedding=[0.1],
            config=_cfg(episodic=False),
            temporal_range=(datetime(2024, 5, 5), datetime(2024, 5, 6)),
        )
        assert [h.fact_id for h in out] == ["f1"]
