"""Tests for Phase 1 innovations — Recall Cache, Memory Hierarchy, Utility Scoring."""

import time

from astrocyte.pipeline.fusion import ScoredItem, layer_weighted_rrf_fusion, rrf_fusion
from astrocyte.pipeline.recall_cache import RecallCache
from astrocyte.pipeline.utility import UtilityTracker, compute_utility
from astrocyte.types import MemoryHit, RecallResult

# ---------------------------------------------------------------------------
# 1.1 Recall Cache
# ---------------------------------------------------------------------------


class TestRecallCache:
    def _make_result(self, texts: list[str]) -> RecallResult:
        return RecallResult(
            hits=[MemoryHit(text=t, score=0.9) for t in texts],
            total_available=len(texts),
            truncated=False,
        )

    def test_cache_miss_returns_none(self):
        cache = RecallCache()
        assert cache.get("bank-1", [1.0, 0.0]) is None

    def test_cache_hit_returns_result(self):
        cache = RecallCache(similarity_threshold=0.95)
        vec = [1.0, 0.0, 0.0]
        result = self._make_result(["memory one"])
        cache.put("bank-1", vec, result)

        # Exact same vector should hit
        cached = cache.get("bank-1", vec)
        assert cached is not None
        assert cached.hits[0].text == "memory one"

    def test_similar_vector_hits(self):
        cache = RecallCache(similarity_threshold=0.9)
        cache.put("bank-1", [1.0, 0.0, 0.0], self._make_result(["cached"]))

        # Very similar vector should hit
        cached = cache.get("bank-1", [0.99, 0.01, 0.0])
        assert cached is not None

    def test_dissimilar_vector_misses(self):
        cache = RecallCache(similarity_threshold=0.95)
        cache.put("bank-1", [1.0, 0.0, 0.0], self._make_result(["cached"]))

        # Orthogonal vector should miss
        cached = cache.get("bank-1", [0.0, 1.0, 0.0])
        assert cached is None

    def test_bank_isolation(self):
        cache = RecallCache()
        cache.put("bank-1", [1.0, 0.0], self._make_result(["bank1"]))

        # Different bank should miss
        assert cache.get("bank-2", [1.0, 0.0]) is None

    def test_invalidate_bank(self):
        cache = RecallCache()
        cache.put("bank-1", [1.0, 0.0], self._make_result(["cached"]))
        assert cache.size("bank-1") == 1

        cache.invalidate_bank("bank-1")
        assert cache.size("bank-1") == 0
        assert cache.get("bank-1", [1.0, 0.0]) is None

    def test_lru_eviction(self):
        cache = RecallCache(max_entries=2)
        cache.put("bank-1", [1.0, 0.0], self._make_result(["first"]))
        cache.put("bank-1", [0.0, 1.0], self._make_result(["second"]))
        cache.put("bank-1", [0.5, 0.5], self._make_result(["third"]))

        # First entry should have been evicted
        assert cache.size("bank-1") == 2

    def test_ttl_expiry(self):
        cache = RecallCache(ttl_seconds=0.01)
        cache.put("bank-1", [1.0, 0.0], self._make_result(["cached"]))

        time.sleep(0.02)
        assert cache.get("bank-1", [1.0, 0.0]) is None

    def test_invalidate_all(self):
        cache = RecallCache()
        cache.put("bank-1", [1.0, 0.0], self._make_result(["one"]))
        cache.put("bank-2", [0.0, 1.0], self._make_result(["two"]))

        cache.invalidate_all()
        assert cache.size() == 0


# ---------------------------------------------------------------------------
# 1.2 Memory Hierarchy — Layer-Weighted Fusion
# ---------------------------------------------------------------------------


class TestLayerWeightedFusion:
    def test_no_weights_same_as_rrf(self):
        items = [ScoredItem(id="a", text="alpha", score=0.9, memory_layer="fact")]
        result_weighted = layer_weighted_rrf_fusion([items], layer_weights=None)
        result_rrf = rrf_fusion([items])
        assert result_weighted[0].score == result_rrf[0].score

    def test_model_layer_boosted_above_fact(self):
        facts = [ScoredItem(id="a", text="fact", score=0.9, memory_layer="fact")]
        models = [ScoredItem(id="b", text="model", score=0.9, memory_layer="model")]

        result = layer_weighted_rrf_fusion(
            [facts, models],
            layer_weights={"fact": 1.0, "model": 2.0},
        )
        # Model should be ranked higher due to 2x weight
        assert result[0].id == "b"
        assert result[0].memory_layer == "model"

    def test_observation_intermediate_weight(self):
        items = [
            ScoredItem(id="f", text="fact", score=0.9, memory_layer="fact"),
            ScoredItem(id="o", text="obs", score=0.9, memory_layer="observation"),
            ScoredItem(id="m", text="model", score=0.9, memory_layer="model"),
        ]
        result = layer_weighted_rrf_fusion(
            [items],
            layer_weights={"fact": 1.0, "observation": 1.5, "model": 2.0},
        )
        # Order should be: model > observation > fact
        assert result[0].id == "m"
        assert result[1].id == "o"
        assert result[2].id == "f"

    def test_none_layer_gets_default_weight(self):
        items = [
            ScoredItem(id="a", text="unlayered", score=0.9, memory_layer=None),
            ScoredItem(id="b", text="model", score=0.9, memory_layer="model"),
        ]
        result = layer_weighted_rrf_fusion(
            [items],
            layer_weights={"model": 2.0},  # No entry for None → weight 1.0
        )
        assert result[0].id == "b"

    def test_memory_layer_preserved_in_scored_item(self):
        items = [ScoredItem(id="a", text="test", score=0.9, memory_layer="observation")]
        result = rrf_fusion([items])
        assert result[0].memory_layer == "observation"

    def test_empty_input(self):
        assert layer_weighted_rrf_fusion([], layer_weights={"fact": 1.0}) == []


# ---------------------------------------------------------------------------
# 1.3 Utility Scoring
# ---------------------------------------------------------------------------


class TestComputeUtility:
    def test_fresh_recently_recalled_high_utility(self):
        score = compute_utility(
            recall_count=10,
            last_recalled_seconds_ago=60,  # 1 minute ago
            avg_relevance=0.9,
            created_seconds_ago=3600,  # 1 hour old
        )
        assert score.composite > 0.5
        assert score.recency > 0.9  # Very recent recall
        assert score.relevance == 0.9

    def test_old_never_recalled_low_utility(self):
        score = compute_utility(
            recall_count=0,
            last_recalled_seconds_ago=86400 * 30,  # 30 days ago
            avg_relevance=0.0,
            created_seconds_ago=86400 * 60,  # 60 days old
        )
        assert score.composite < 0.3
        assert score.frequency == 0.0
        assert score.relevance == 0.0

    def test_frequently_recalled_high_frequency(self):
        score = compute_utility(
            recall_count=50,
            last_recalled_seconds_ago=3600,
            avg_relevance=0.7,
            created_seconds_ago=86400 * 7,
        )
        assert score.frequency == 0.5  # 50/100

    def test_max_frequency_capped(self):
        score = compute_utility(
            recall_count=200,
            last_recalled_seconds_ago=0,
            avg_relevance=1.0,
            created_seconds_ago=0,
        )
        assert score.frequency == 1.0  # Capped at 1.0

    def test_composite_in_range(self):
        score = compute_utility(
            recall_count=5,
            last_recalled_seconds_ago=3600,
            avg_relevance=0.5,
            created_seconds_ago=86400,
        )
        assert 0.0 <= score.composite <= 1.0
        assert 0.0 <= score.recency <= 1.0
        assert 0.0 <= score.frequency <= 1.0
        assert 0.0 <= score.freshness <= 1.0

    def test_relevance_clamped(self):
        score = compute_utility(
            recall_count=1,
            last_recalled_seconds_ago=0,
            avg_relevance=1.5,  # Out of range
            created_seconds_ago=0,
        )
        assert score.relevance == 1.0  # Clamped

    def test_zero_everything(self):
        score = compute_utility(
            recall_count=0,
            last_recalled_seconds_ago=0,
            avg_relevance=0.0,
            created_seconds_ago=0,
        )
        assert score.composite >= 0.0  # Should not crash


class TestUtilityTracker:
    def test_record_recall(self):
        tracker = UtilityTracker()
        tracker.record_creation("m1")
        tracker.record_recall("m1", 0.8)
        tracker.record_recall("m1", 0.9)

        assert tracker.get_recall_count("m1") == 2
        utility = tracker.get_utility("m1")
        assert utility is not None
        assert utility.frequency > 0
        assert utility.recency > 0.9  # Just recalled

    def test_untracked_returns_none(self):
        tracker = UtilityTracker()
        assert tracker.get_utility("nonexistent") is None

    def test_lru_eviction(self):
        tracker = UtilityTracker(max_entries=2)
        tracker.record_creation("m1")
        tracker.record_creation("m2")
        tracker.record_creation("m3")

        # m1 should have been evicted
        assert tracker.get_utility("m1") is None
        assert tracker.get_utility("m2") is not None
        assert tracker.get_utility("m3") is not None

    def test_clear(self):
        tracker = UtilityTracker()
        tracker.record_creation("m1")
        tracker.record_recall("m1", 0.5)
        tracker.clear()
        assert tracker.get_utility("m1") is None

    def test_recall_count(self):
        tracker = UtilityTracker()
        assert tracker.get_recall_count("m1") == 0
        tracker.record_recall("m1", 0.5)
        assert tracker.get_recall_count("m1") == 1
        tracker.record_recall("m1", 0.6)
        assert tracker.get_recall_count("m1") == 2


# ---------------------------------------------------------------------------
# Type field additions
# ---------------------------------------------------------------------------


class TestNewTypeFields:
    def test_vector_item_memory_layer(self):
        from astrocyte.types import VectorItem

        v = VectorItem(id="v1", bank_id="b1", vector=[0.1], text="test", memory_layer="observation")
        assert v.memory_layer == "observation"

    def test_vector_item_memory_layer_default_none(self):
        from astrocyte.types import VectorItem

        v = VectorItem(id="v1", bank_id="b1", vector=[0.1], text="test")
        assert v.memory_layer is None

    def test_memory_hit_utility_score(self):
        h = MemoryHit(text="test", score=0.9, utility_score=0.75)
        assert h.utility_score == 0.75

    def test_memory_hit_memory_layer(self):
        h = MemoryHit(text="test", score=0.9, memory_layer="model")
        assert h.memory_layer == "model"

    def test_recall_request_layer_weights(self):
        from astrocyte.types import RecallRequest

        r = RecallRequest(query="test", bank_id="b1", layer_weights={"fact": 1.0, "model": 2.0})
        assert r.layer_weights["model"] == 2.0

    def test_recall_request_detail_level(self):
        from astrocyte.types import RecallRequest

        r = RecallRequest(query="test", bank_id="b1", detail_level="titles")
        assert r.detail_level == "titles"

    def test_recall_request_external_context(self):
        from astrocyte.types import RecallRequest

        ext = [MemoryHit(text="external", score=0.8)]
        r = RecallRequest(query="test", bank_id="b1", external_context=ext)
        assert len(r.external_context) == 1

    def test_recall_trace_new_fields(self):
        from astrocyte.types import RecallTrace

        t = RecallTrace(tier_used=2, layer_distribution={"fact": 5, "model": 1}, cache_hit=True)
        assert t.tier_used == 2
        assert t.cache_hit is True

    def test_retain_result_curation_fields(self):
        from astrocyte.types import RetainResult

        r = RetainResult(stored=True, retention_action="merge", curated=True, memory_layer="observation")
        assert r.retention_action == "merge"
        assert r.curated is True
        assert r.memory_layer == "observation"

    def test_all_new_fields_default_none(self):
        """All new fields should be backward compatible with None/False defaults."""
        from astrocyte.types import RecallRequest, RecallTrace, RetainResult, VectorHit, VectorItem

        v = VectorItem(id="v", bank_id="b", vector=[0.1], text="t")
        assert v.memory_layer is None

        vh = VectorHit(id="v", text="t", score=0.5)
        assert vh.memory_layer is None

        h = MemoryHit(text="t", score=0.5)
        assert h.memory_layer is None
        assert h.utility_score is None

        r = RecallRequest(query="q", bank_id="b")
        assert r.layer_weights is None
        assert r.detail_level is None
        assert r.external_context is None

        t = RecallTrace()
        assert t.tier_used is None
        assert t.layer_distribution is None
        assert t.cache_hit is None

        rr = RetainResult(stored=True)
        assert rr.retention_action is None
        assert rr.curated is False
        assert rr.memory_layer is None
