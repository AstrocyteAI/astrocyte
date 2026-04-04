"""Tests for pipeline/fusion.py — Reciprocal Rank Fusion."""

from astrocyte.pipeline.fusion import ScoredItem, rrf_fusion


class TestRRFFusion:
    def test_empty_input(self):
        assert rrf_fusion([]) == []

    def test_single_list(self):
        items = [ScoredItem(id="a", text="alpha", score=0.9), ScoredItem(id="b", text="beta", score=0.8)]
        result = rrf_fusion([items])
        assert len(result) == 2
        assert result[0].id == "a"  # Higher rank

    def test_two_lists_boost_overlap(self):
        list1 = [ScoredItem(id="a", text="alpha", score=0.9), ScoredItem(id="b", text="beta", score=0.8)]
        list2 = [ScoredItem(id="b", text="beta", score=0.7), ScoredItem(id="c", text="gamma", score=0.6)]
        result = rrf_fusion([list1, list2], k=60)

        # "b" appears in both lists — should get boosted
        ids = [r.id for r in result]
        assert "b" in ids
        b_item = next(r for r in result if r.id == "b")
        a_item = next(r for r in result if r.id == "a")
        # b should have higher RRF score than a (appears in 2 lists vs 1)
        assert b_item.score > a_item.score

    def test_deduplication(self):
        list1 = [ScoredItem(id="a", text="alpha", score=0.9)]
        list2 = [ScoredItem(id="a", text="alpha", score=0.7)]
        result = rrf_fusion([list1, list2])
        # Should deduplicate — only one item
        assert len(result) == 1
        assert result[0].id == "a"

    def test_preserves_metadata(self):
        items = [ScoredItem(id="a", text="alpha", score=0.9, fact_type="world", tags=["test"])]
        result = rrf_fusion([items])
        assert result[0].fact_type == "world"
        assert result[0].tags == ["test"]

    def test_k_parameter(self):
        items = [ScoredItem(id="a", text="alpha", score=0.9)]
        result_k1 = rrf_fusion([items], k=1)
        result_k100 = rrf_fusion([items], k=100)
        # Higher k = lower individual RRF scores
        assert result_k1[0].score > result_k100[0].score

    def test_three_lists(self):
        list1 = [ScoredItem(id="a", text="alpha", score=0.9)]
        list2 = [ScoredItem(id="a", text="alpha", score=0.8)]
        list3 = [ScoredItem(id="a", text="alpha", score=0.7)]
        result = rrf_fusion([list1, list2, list3], k=60)
        # Item in all 3 lists gets 3x RRF contribution
        assert len(result) == 1
        single_list_result = rrf_fusion([list1], k=60)
        assert result[0].score > single_list_result[0].score
