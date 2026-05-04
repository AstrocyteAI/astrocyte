"""Temporal retrieval strategy tests.

Covers the new ``_temporal_search`` strategy added to ``parallel_retrieve``
(see ``docs/_design/platform-positioning.md`` §Mystique — 4-way retrieval
inspired by Hindsight). The strategy ranks bank vectors by recency decay
so that recently-retained memories surface even when they lose the
semantic cutoff to older near-matches. RRF in the orchestrator fuses this
with the semantic/keyword/graph strategies.

Contract pinned by these tests:

1. Strategy no-ops gracefully when no timestamps are present (returns
   empty list, fuses harmlessly).
2. ``occurred_at`` event/session time is the primary ranking signal;
   ``_created_at`` metadata is a fallback when event time is absent.
3. Exponential decay over ``half_life_days`` — a memory exactly one
   half-life old scores 0.5, fresh scores approach 1.0.
4. Scan cap bounds cost on large banks — the strategy walks paginated
   ``list_vectors`` and stops at the cap.
5. RRF fusion in the orchestrator treats the strategy as an independent
   rank input; recency rescues can outvote semantic distractors.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from astrocyte.pipeline.fusion import ScoredItem, rrf_fusion
from astrocyte.pipeline.retrieval import (
    DEFAULT_TEMPORAL_HALF_LIFE_DAYS,
    _extract_timestamp,
    _temporal_search,
    parallel_retrieve,
)
from astrocyte.pipeline.temporal import normalize_relative_temporal_facts, temporal_metadata
from astrocyte.testing.in_memory import InMemoryVectorStore
from astrocyte.types import DocumentHit, VectorHit, VectorItem


def _vec(id_: str, text: str, bank: str = "b1", *, created_at: datetime | None = None,
         occurred_at: datetime | None = None) -> VectorItem:
    meta: dict = {}
    if created_at is not None:
        meta["_created_at"] = created_at.isoformat()
    return VectorItem(
        id=id_,
        bank_id=bank,
        vector=[1.0, 0.0],
        text=text,
        metadata=meta or None,
        occurred_at=occurred_at,
    )


# ---------------------------------------------------------------------------
# _temporal_search — core ranking contract
# ---------------------------------------------------------------------------


class TestTemporalRanking:
    async def test_empty_bank_returns_empty(self) -> None:
        store = InMemoryVectorStore()
        out = await _temporal_search(
            store, "b1", limit=10, scan_cap=500, half_life_days=7.0,
        )
        assert out == []

    def test_normalizes_relative_phrase_against_session_date(self) -> None:
        anchor = datetime(2026, 2, 10, tzinfo=timezone.utc)

        facts = normalize_relative_temporal_facts("Max learned to sit last week.", anchor)

        assert facts[0].phrase == "last week"
        assert facts[0].resolved_date == "2026-02-03"
        assert facts[0].granularity == "week"

    def test_temporal_metadata_is_metadata_safe(self) -> None:
        anchor = datetime(2026, 2, 10, tzinfo=timezone.utc)

        metadata = temporal_metadata("Alice went yesterday.", anchor)

        assert metadata == {
            "temporal_anchor": "2026-02-10",
            "temporal_phrase": "yesterday",
            "resolved_date": "2026-02-09",
            "date_granularity": "day",
        }

    async def test_bank_with_no_timestamps_returns_empty(self) -> None:
        """If no vector has a usable timestamp, the strategy must no-op
        (not rank them all as 'infinitely old'). RRF will ignore the
        empty list so semantic-only retrieval stays correct."""
        store = InMemoryVectorStore()
        await store.store_vectors([
            _vec("v1", "hello"),
            _vec("v2", "world"),
        ])
        out = await _temporal_search(
            store, "b1", limit=10, scan_cap=500, half_life_days=7.0,
        )
        assert out == []

    async def test_recency_order_by_created_at(self) -> None:
        """Most-recent memory ranks first regardless of insertion order."""
        now = datetime.now(timezone.utc)
        store = InMemoryVectorStore()
        await store.store_vectors([
            _vec("old", "old memory", created_at=now - timedelta(days=30)),
            _vec("fresh", "fresh memory", created_at=now - timedelta(hours=1)),
            _vec("mid", "mid memory", created_at=now - timedelta(days=3)),
        ])
        out = await _temporal_search(
            store, "b1", limit=10, scan_cap=500, half_life_days=7.0,
        )
        assert [item.id for item in out] == ["fresh", "mid", "old"]

    async def test_occurred_at_fallback_when_created_at_missing(self) -> None:
        """``occurred_at`` is a usable timestamp — a VectorItem
        without ``_created_at`` metadata but with ``occurred_at`` still
        gets ranked."""
        now = datetime.now(timezone.utc)
        store = InMemoryVectorStore()
        await store.store_vectors([
            _vec("with-occurred", "x", occurred_at=now - timedelta(hours=2)),
            _vec("no-timestamp", "y"),
        ])
        out = await _temporal_search(
            store, "b1", limit=10, scan_cap=500, half_life_days=7.0,
        )
        # Only the item with a usable timestamp contributes.
        assert [item.id for item in out] == ["with-occurred"]

    def test_occurred_at_preferred_over_created_at(self) -> None:
        now = datetime.now(timezone.utc)
        item = _vec(
            "event-time",
            "x",
            created_at=now,
            occurred_at=now - timedelta(days=10),
        )
        assert _extract_timestamp(item) == item.occurred_at

    async def test_exponential_decay_half_life(self) -> None:
        """A memory exactly one half-life old must score ~0.5; two
        half-lives old must score ~0.25. Pins the decay formula so a
        regression on the math would fail loudly."""
        now = datetime.now(timezone.utc)
        half_life = 7.0
        store = InMemoryVectorStore()
        await store.store_vectors([
            _vec("fresh", "a", created_at=now - timedelta(seconds=1)),
            _vec("one_hl", "b", created_at=now - timedelta(days=half_life)),
            _vec("two_hl", "c", created_at=now - timedelta(days=half_life * 2)),
        ])
        out = await _temporal_search(
            store, "b1", limit=10, scan_cap=500, half_life_days=half_life,
        )
        by_id = {item.id: item.score for item in out}
        assert by_id["fresh"] == pytest.approx(1.0, abs=0.01)
        assert by_id["one_hl"] == pytest.approx(0.5, abs=0.01)
        assert by_id["two_hl"] == pytest.approx(0.25, abs=0.01)

    async def test_scan_cap_bounds_cost(self) -> None:
        """scan_cap bounds how many vectors the strategy inspects. With
        a cap of 2 and 5 vectors in the bank, only the first-2-by-ID
        are scored — which may not be the newest. The contract is NOT
        'always get the top N newest'; it is 'walk up to scan_cap and
        rank those'. Operators trading accuracy for cost on huge banks
        are responsible for the trade-off."""
        now = datetime.now(timezone.utc)
        store = InMemoryVectorStore()
        # list_vectors returns sorted by id — use ids that sort to put
        # the fresh one LAST so it's excluded when scan_cap=2.
        await store.store_vectors([
            _vec("a-old", "a", created_at=now - timedelta(days=30)),
            _vec("b-oldish", "b", created_at=now - timedelta(days=10)),
            _vec("c-fresh", "c", created_at=now - timedelta(hours=1)),
        ])
        out = await _temporal_search(
            store, "b1", limit=10, scan_cap=2, half_life_days=7.0,
        )
        ids = {item.id for item in out}
        assert "c-fresh" not in ids  # cap excluded it
        assert ids == {"a-old", "b-oldish"}

    async def test_limit_trims_output(self) -> None:
        now = datetime.now(timezone.utc)
        store = InMemoryVectorStore()
        await store.store_vectors([
            _vec(f"v{i}", f"t{i}", created_at=now - timedelta(hours=i))
            for i in range(10)
        ])
        out = await _temporal_search(
            store, "b1", limit=3, scan_cap=500, half_life_days=7.0,
        )
        assert len(out) == 3

    async def test_preserves_metadata_and_tags(self) -> None:
        """ScoredItem output must carry through metadata/tags/fact_type
        so downstream rerank + reflect can still reason about the hit."""
        now = datetime.now(timezone.utc)
        store = InMemoryVectorStore()
        await store.store_vectors([
            VectorItem(
                id="v1", bank_id="b1", vector=[1.0, 0.0], text="t",
                metadata={"_created_at": now.isoformat(), "k": "v"},
                tags=["topic:x"],
                fact_type="world",
                memory_layer="fact",
            ),
        ])
        out = await _temporal_search(
            store, "b1", limit=10, scan_cap=500, half_life_days=7.0,
        )
        assert len(out) == 1
        item = out[0]
        assert item.metadata is not None and item.metadata.get("k") == "v"
        assert item.tags == ["topic:x"]
        assert item.fact_type == "world"
        assert item.memory_layer == "fact"


# ---------------------------------------------------------------------------
# _extract_timestamp — tolerance to shapes
# ---------------------------------------------------------------------------


class TestExtractTimestamp:
    def test_iso_string_created_at(self) -> None:
        now = datetime(2026, 4, 18, 12, tzinfo=timezone.utc)
        v = VectorItem(
            id="v", bank_id="b", vector=[0.0], text="t",
            metadata={"_created_at": now.isoformat()},
        )
        assert _extract_timestamp(v) == now

    def test_malformed_iso_falls_back_to_occurred_at(self) -> None:
        """Bad ISO strings shouldn't crash the whole strategy. Fall
        back to occurred_at if the metadata is garbage."""
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        v = VectorItem(
            id="v", bank_id="b", vector=[0.0], text="t",
            metadata={"_created_at": "not-a-date"},
            occurred_at=now,
        )
        assert _extract_timestamp(v) == now

    def test_naive_datetime_interpreted_as_utc(self) -> None:
        naive = datetime(2026, 4, 18, 12)
        v = VectorItem(
            id="v", bank_id="b", vector=[0.0], text="t",
            metadata=None,
            occurred_at=naive,
        )
        result = _extract_timestamp(v)
        assert result is not None
        assert result.tzinfo is timezone.utc
        assert result.replace(tzinfo=None) == naive

    def test_no_timestamps_returns_none(self) -> None:
        v = VectorItem(id="v", bank_id="b", vector=[0.0], text="t")
        assert _extract_timestamp(v) is None


# ---------------------------------------------------------------------------
# parallel_retrieve integration — temporal joins the RRF inputs
# ---------------------------------------------------------------------------


class TestParallelRetrieveTemporal:
    async def test_temporal_strategy_appears_when_enabled(self) -> None:
        now = datetime.now(timezone.utc)
        store = InMemoryVectorStore()
        await store.store_vectors([
            _vec("v1", "x", created_at=now),
        ])
        results = await parallel_retrieve(
            query_vector=[1.0, 0.0],
            query_text="x",
            bank_id="b1",
            vector_store=store,
            enable_temporal=True,
        )
        assert "temporal" in results
        assert len(results["temporal"]) == 1

    async def test_temporal_disabled_by_flag(self) -> None:
        now = datetime.now(timezone.utc)
        store = InMemoryVectorStore()
        await store.store_vectors([_vec("v1", "x", created_at=now)])
        results = await parallel_retrieve(
            query_vector=[1.0, 0.0],
            query_text="x",
            bank_id="b1",
            vector_store=store,
            enable_temporal=False,
        )
        assert "temporal" not in results

    async def test_half_life_knob_flows_through(self) -> None:
        """Passing a shorter half-life should change the score that
        the strategy produces for the same fixture."""
        now = datetime.now(timezone.utc)
        store = InMemoryVectorStore()
        await store.store_vectors([
            _vec("old", "x", created_at=now - timedelta(days=7)),
        ])
        slow = await parallel_retrieve(
            query_vector=[1.0, 0.0], query_text="x", bank_id="b1",
            vector_store=store, temporal_half_life_days=30.0,
        )
        fast = await parallel_retrieve(
            query_vector=[1.0, 0.0], query_text="x", bank_id="b1",
            vector_store=store, temporal_half_life_days=1.0,
        )
        # Faster decay → 7-day-old memory scored much lower.
        assert slow["temporal"][0].score > fast["temporal"][0].score

    async def test_pgvector_hybrid_fast_path_populates_semantic_keyword_and_trace(self) -> None:
        class HybridStore(InMemoryVectorStore):
            def __init__(self) -> None:
                super().__init__()
                self.hybrid_called = False

            async def search_hybrid_semantic_bm25(self, *_args, **_kwargs):
                self.hybrid_called = True
                return {
                    "semantic": [
                        VectorHit(id="sem", text="semantic hit", score=0.9),
                    ],
                    "keyword": [
                        DocumentHit(document_id="kw", text="keyword hit", score=0.8),
                    ],
                }

        store = HybridStore()
        timings: dict[str, float] = {}
        counts: dict[str, int] = {}

        results = await parallel_retrieve(
            query_vector=[1.0, 0.0],
            query_text="keyword",
            bank_id="b1",
            vector_store=store,
            document_store=store,
            enable_temporal=False,
            strategy_timings_ms=timings,
            strategy_candidate_counts=counts,
        )

        assert store.hybrid_called is True
        assert [item.id for item in results["semantic"]] == ["sem"]
        assert [item.id for item in results["keyword"]] == ["kw"]
        assert set(timings) == {"semantic", "keyword"}
        assert counts == {"semantic": 1, "keyword": 1}

    async def test_hybrid_failure_falls_back_to_per_strategy(self) -> None:
        """When the hybrid CTE throws, parallel_retrieve MUST fall back to
        running semantic + keyword as separate strategies — NOT clobber
        both to []. A single transient DB hiccup (pool exhaustion,
        deadlock, lock-wait timeout) on the hybrid path otherwise turns
        into a total recall failure for the entire question.

        Regression for the P0 bug from the architecture review: the prior
        ``except Exception`` block set ``results['semantic'] = []`` and
        ``results['keyword'] = []`` directly.
        """

        class HybridFailsStore(InMemoryVectorStore):
            def __init__(self) -> None:
                super().__init__()
                self.hybrid_attempted = False
                self.semantic_fallback_called = False
                self.keyword_fallback_called = False
                # Seed one vector for the semantic fallback to find.
                # Use sync-friendly setup (no await in __init__).

            async def search_hybrid_semantic_bm25(self, *_args, **_kwargs):
                self.hybrid_attempted = True
                raise RuntimeError("simulated transient pool error")

            async def search_similar(self, query_vector, bank_id, **kwargs):
                self.semantic_fallback_called = True
                return [
                    VectorHit(id="fallback-sem", text="recovered semantic hit", score=0.7),
                ]

            async def search_fulltext(self, query, bank_id, **kwargs):
                self.keyword_fallback_called = True
                return [
                    DocumentHit(document_id="fallback-kw", text="recovered keyword hit", score=0.6),
                ]

        store = HybridFailsStore()
        timings: dict[str, float] = {}
        counts: dict[str, int] = {}

        results = await parallel_retrieve(
            query_vector=[1.0, 0.0],
            query_text="recover",
            bank_id="b1",
            vector_store=store,
            document_store=store,
            enable_temporal=False,
            strategy_timings_ms=timings,
            strategy_candidate_counts=counts,
        )

        # Hybrid was attempted (and failed).
        assert store.hybrid_attempted is True
        # Both fallback paths fired.
        assert store.semantic_fallback_called is True
        assert store.keyword_fallback_called is True
        # CRITICAL: results are populated from the fallback, NOT empty.
        assert [item.id for item in results["semantic"]] == ["fallback-sem"]
        assert [item.id for item in results["keyword"]] == ["fallback-kw"]
        # Timings reflect the fallback execution (not the failed hybrid time).
        assert "semantic" in timings and "keyword" in timings
        assert counts == {"semantic": 1, "keyword": 1}

    async def test_use_bm25_idf_routes_to_search_fulltext_bm25(self) -> None:
        """When ``use_bm25_idf=True`` AND the store advertises
        ``search_fulltext_bm25``, the keyword strategy must call THAT
        method, not the classic ``search_fulltext``. Stores without the
        method silently fall through to the classic path."""

        class Bm25Store(InMemoryVectorStore):
            def __init__(self) -> None:
                super().__init__()
                self.classic_called = False
                self.bm25_called = False

            async def search_fulltext(self, *_args, **_kwargs):
                self.classic_called = True
                return [DocumentHit(document_id="classic", text="x", score=0.5)]

            async def search_fulltext_bm25(self, *_args, **_kwargs):
                self.bm25_called = True
                return [DocumentHit(document_id="bm25", text="x", score=0.9)]

        store = Bm25Store()

        # Path A: use_bm25_idf=True → bm25 method called, classic NOT called.
        # Skip the hybrid path by passing a separate document_store and a
        # zero query_vector — it's the simplest way to force per-strategy.
        results = await parallel_retrieve(
            query_vector=[1.0, 0.0],
            query_text="anything",
            bank_id="b1",
            vector_store=InMemoryVectorStore(),  # different instance — no hybrid
            document_store=store,
            enable_temporal=False,
            use_bm25_idf=True,
        )
        assert store.bm25_called is True
        assert store.classic_called is False
        assert [item.id for item in results["keyword"]] == ["bm25"]

    async def test_use_bm25_idf_falls_through_for_stores_without_method(self) -> None:
        """Stores that don't expose ``search_fulltext_bm25`` (in_memory,
        elasticsearch, etc.) must use ``search_fulltext`` even when the
        flag is on — the flag is best-effort, not strict."""

        class ClassicOnlyStore(InMemoryVectorStore):
            def __init__(self) -> None:
                super().__init__()
                self.classic_called = False

            async def search_fulltext(self, *_args, **_kwargs):
                self.classic_called = True
                return [DocumentHit(document_id="classic", text="x", score=0.5)]
            # NO search_fulltext_bm25 — use_bm25_idf must fall through.

        store = ClassicOnlyStore()
        results = await parallel_retrieve(
            query_vector=[1.0, 0.0],
            query_text="anything",
            bank_id="b1",
            vector_store=InMemoryVectorStore(),
            document_store=store,
            enable_temporal=False,
            use_bm25_idf=True,
        )
        assert store.classic_called is True
        assert [item.id for item in results["keyword"]] == ["classic"]

    async def test_hybrid_failure_isolates_per_strategy_fallback_failure(self) -> None:
        """If the hybrid CTE fails AND one of the two fallback strategies
        ALSO fails (e.g. semantic-search hits the same DB error), the
        OTHER fallback must still flow through. Per-strategy isolation
        all the way down."""

        class HybridFailsStore(InMemoryVectorStore):
            async def search_hybrid_semantic_bm25(self, *_args, **_kwargs):
                raise RuntimeError("hybrid CTE died")

            async def search_similar(self, query_vector, bank_id, **kwargs):
                # Semantic fallback also fails — simulating a deeper outage.
                raise RuntimeError("semantic also down")

            async def search_fulltext(self, query, bank_id, **kwargs):
                # Keyword fallback succeeds.
                return [DocumentHit(document_id="kw-survived", text="x", score=0.5)]

        store = HybridFailsStore()
        results = await parallel_retrieve(
            query_vector=[1.0, 0.0],
            query_text="survive",
            bank_id="b1",
            vector_store=store,
            document_store=store,
            enable_temporal=False,
        )

        # Semantic fallback failed → empty list (isolated, not exception).
        assert results["semantic"] == []
        # Keyword fallback succeeded → results survive (wrapped as ScoredItem).
        assert [item.id for item in results["keyword"]] == ["kw-survived"]


# ---------------------------------------------------------------------------
# RRF fusion — temporal can outvote semantic distractors
# ---------------------------------------------------------------------------


class TestRRFRescueFromTemporal:
    def test_temporal_rank_one_beats_semantic_rank_two(self) -> None:
        """End-to-end: a memory ranked #1 by temporal but #4 by semantic
        can outvote a memory ranked #1 by semantic alone. This is the
        'recency rescue' hit-rate lift the strategy exists to produce."""
        # Semantic ranks: distractor > relevant > a > b > c
        semantic = [
            ScoredItem(id="distractor", text="older-but-similar", score=0.95),
            ScoredItem(id="relevant", text="recent-answer", score=0.90),
            ScoredItem(id="a", text="a", score=0.80),
            ScoredItem(id="b", text="b", score=0.70),
        ]
        # Temporal ranks: relevant > a > b > distractor (distractor is old)
        temporal = [
            ScoredItem(id="relevant", text="recent-answer", score=1.0),
            ScoredItem(id="a", text="a", score=0.9),
            ScoredItem(id="b", text="b", score=0.8),
            ScoredItem(id="distractor", text="older-but-similar", score=0.1),
        ]

        fused = rrf_fusion([semantic, temporal], k=60)
        ids_in_order = [item.id for item in fused]
        # "relevant" ranks before "distractor" now — temporal broke the tie.
        assert ids_in_order.index("relevant") < ids_in_order.index("distractor")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_default_half_life_is_sensible() -> None:
    """Pin the documented default so a quiet change would fail a test.
    Deployments relying on 'a week feels recent' would silently shift
    if this drifted."""
    assert DEFAULT_TEMPORAL_HALF_LIFE_DAYS == 7.0
