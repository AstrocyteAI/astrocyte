"""Tests for pipeline modules — fusion, reranking, and consolidation.

Covers layer-weighted RRF, memory_hits_as_scored, proper noun boosting,
tokenization, _VectorBuckets, and the full consolidation flow.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from astrocyte.pipeline.consolidation import (
    _parse_dt,
    _VectorBuckets,
    run_consolidation,
)
from astrocyte.pipeline.fusion import (
    ScoredItem,
    layer_weighted_rrf_fusion,
    memory_hits_as_scored,
    rrf_fusion,
)
from astrocyte.pipeline.reranking import (
    _is_name_token,
    _tokenize_terms,
    basic_rerank,
)
from astrocyte.testing.in_memory import InMemoryVectorStore
from astrocyte.types import MemoryHit, VectorItem

# ---------------------------------------------------------------------------
# Fusion — layer_weighted_rrf_fusion
# ---------------------------------------------------------------------------


class TestLayerWeightedRRFFusion:
    def test_no_weights_same_as_rrf(self):
        items = [ScoredItem(id="a", text="alpha", score=0.9)]
        plain = rrf_fusion([items])
        weighted = layer_weighted_rrf_fusion([items], layer_weights=None)
        assert plain[0].score == weighted[0].score

    def test_layer_boost_reranks(self):
        items = [
            ScoredItem(id="a", text="fact", score=0.9, memory_layer="fact"),
            ScoredItem(id="b", text="model", score=0.8, memory_layer="model"),
        ]
        result = layer_weighted_rrf_fusion(
            [items], layer_weights={"fact": 1.0, "model": 3.0}
        )
        # "model" gets 3x boost — should rank first despite lower original score
        assert result[0].id == "b"

    def test_missing_layer_gets_weight_1(self):
        items = [ScoredItem(id="a", text="no layer", score=0.9)]
        result = layer_weighted_rrf_fusion(
            [items], layer_weights={"fact": 2.0}
        )
        # No layer → weight 1.0, same as plain RRF
        plain = rrf_fusion([items])
        assert result[0].score == plain[0].score

    def test_empty_input(self):
        assert layer_weighted_rrf_fusion([]) == []

    def test_preserves_metadata(self):
        items = [
            ScoredItem(id="a", text="t", score=0.9, fact_type="world",
                       tags=["x"], memory_layer="fact")
        ]
        result = layer_weighted_rrf_fusion([items], layer_weights={"fact": 2.0})
        assert result[0].fact_type == "world"
        assert result[0].tags == ["x"]
        assert result[0].memory_layer == "fact"


# ---------------------------------------------------------------------------
# Fusion — memory_hits_as_scored
# ---------------------------------------------------------------------------


class TestMemoryHitsAsScored:
    def test_converts_with_id(self):
        hits = [MemoryHit(text="hello", score=0.9, memory_id="m1")]
        scored = memory_hits_as_scored(hits)
        assert len(scored) == 1
        assert scored[0].id == "m1"
        assert scored[0].text == "hello"
        assert scored[0].score == 0.9

    def test_generates_id_when_missing(self):
        hits = [MemoryHit(text="hello", score=0.9)]
        scored = memory_hits_as_scored(hits)
        assert scored[0].id.startswith("ext-")

    def test_deterministic_id_for_same_text(self):
        hits1 = [MemoryHit(text="same content", score=0.5)]
        hits2 = [MemoryHit(text="same content", score=0.7)]
        assert memory_hits_as_scored(hits1)[0].id == memory_hits_as_scored(hits2)[0].id

    def test_preserves_fields(self):
        hits = [MemoryHit(
            text="t", score=0.5, fact_type="world",
            tags=["a"], memory_layer="fact", metadata={"k": "v"},
        )]
        scored = memory_hits_as_scored(hits)
        assert scored[0].fact_type == "world"
        assert scored[0].tags == ["a"]
        assert scored[0].memory_layer == "fact"
        assert scored[0].metadata == {"k": "v"}

    def test_empty_input(self):
        assert memory_hits_as_scored([]) == []


# ---------------------------------------------------------------------------
# Reranking — _tokenize_terms
# ---------------------------------------------------------------------------


class TestTokenizeTerms:
    def test_basic(self):
        assert _tokenize_terms("Hello World") == ["hello", "world"]

    def test_strips_punctuation(self):
        result = _tokenize_terms("hello, world!")
        assert "hello" in result
        assert "world" in result

    def test_empty_string(self):
        assert _tokenize_terms("") == []

    def test_only_punctuation(self):
        assert _tokenize_terms("!!! ???") == []


# ---------------------------------------------------------------------------
# Reranking — _is_name_token
# ---------------------------------------------------------------------------


class TestIsNameToken:
    def test_simple_name(self):
        assert _is_name_token("Alice") is True

    def test_apostrophe_name(self):
        assert _is_name_token("O'Brien") is True

    def test_hyphenated_name(self):
        assert _is_name_token("Mary-Ann") is True

    def test_number(self):
        assert _is_name_token("123") is False

    def test_empty(self):
        assert _is_name_token("") is False

    def test_starts_with_punctuation(self):
        assert _is_name_token("'hello") is False

    def test_ends_with_punctuation(self):
        assert _is_name_token("hello-") is False

    def test_only_connectors(self):
        assert _is_name_token("--") is False


# ---------------------------------------------------------------------------
# Reranking — basic_rerank (proper noun boosting)
# ---------------------------------------------------------------------------


class TestBasicRerankProperNouns:
    def test_proper_noun_boosted(self):
        items = [
            ScoredItem(id="a", text="the cat sat on the mat", score=0.5),
            ScoredItem(id="b", text="Alice went to the store", score=0.5),
        ]
        result = basic_rerank(items, "What did Alice do?")
        assert result[0].id == "b"

    def test_all_caps_entity_boosted(self):
        items = [
            ScoredItem(id="a", text="random text", score=0.5),
            ScoredItem(id="b", text="I work at NASA", score=0.5),
        ]
        result = basic_rerank(items, "Tell me about NASA")
        assert result[0].id == "b"

    def test_query_terms_filtered(self):
        """Common question words (what, does, etc.) should not count as keywords."""
        items = [
            ScoredItem(id="a", text="what does it do", score=0.5),
            ScoredItem(id="b", text="dark mode preference", score=0.5),
        ]
        result = basic_rerank(items, "what does dark mode do")
        # "dark" and "mode" should boost b; "what", "does", "do" are filtered
        assert result[0].id == "b"

    def test_preserves_original_ordering_when_no_boost(self):
        items = [
            ScoredItem(id="a", text="alpha", score=0.9),
            ScoredItem(id="b", text="beta", score=0.8),
        ]
        result = basic_rerank(items, "unrelated query xyz")
        assert result[0].id == "a"


# ---------------------------------------------------------------------------
# Consolidation — _VectorBuckets
# ---------------------------------------------------------------------------


class TestVectorBuckets:
    def test_find_similar_identical(self):
        buckets = _VectorBuckets()
        vec = [1.0, 0.0, 0.0, 0.0]
        buckets.add("v1", vec)
        assert buckets.find_similar(vec, threshold=0.99) is True

    def test_find_similar_orthogonal(self):
        buckets = _VectorBuckets()
        buckets.add("v1", [1.0, 0.0, 0.0, 0.0])
        assert buckets.find_similar([0.0, 1.0, 0.0, 0.0], threshold=0.5) is False

    def test_find_similar_empty(self):
        buckets = _VectorBuckets()
        assert buckets.find_similar([1.0, 0.0], threshold=0.5) is False

    def test_multiple_items(self):
        buckets = _VectorBuckets()
        buckets.add("v1", [1.0, 0.0])
        buckets.add("v2", [0.0, 1.0])
        # Near-duplicate of v1
        assert buckets.find_similar([0.99, 0.01], threshold=0.9) is True


# ---------------------------------------------------------------------------
# Consolidation — _parse_dt
# ---------------------------------------------------------------------------


class TestParseDt:
    def test_valid_iso(self):
        dt = _parse_dt("2025-01-15T10:30:00+00:00")
        assert dt is not None
        assert dt.year == 2025

    def test_invalid_string(self):
        assert _parse_dt("not a date") is None

    def test_date_only(self):
        dt = _parse_dt("2025-01-15")
        assert dt is not None
        assert dt.day == 15


# ---------------------------------------------------------------------------
# Consolidation — run_consolidation
# ---------------------------------------------------------------------------


class TestRunConsolidation:
    @pytest.mark.asyncio
    async def test_dedup_removes_near_duplicates(self):
        vs = InMemoryVectorStore()
        # Store two near-identical vectors
        await vs.store_vectors([
            VectorItem(id="v1", text="hello world", vector=[1.0, 0.0, 0.0, 0.0], bank_id="b1"),
            VectorItem(id="v2", text="hello world copy", vector=[1.0, 0.001, 0.0, 0.0], bank_id="b1"),
        ])
        result = await run_consolidation(vs, "b1", similarity_threshold=0.99)
        assert result.total_scanned == 2
        assert result.duplicates_removed == 1

    @pytest.mark.asyncio
    async def test_no_duplicates(self):
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            VectorItem(id="v1", text="a", vector=[1.0, 0.0], bank_id="b1"),
            VectorItem(id="v2", text="b", vector=[0.0, 1.0], bank_id="b1"),
        ])
        result = await run_consolidation(vs, "b1", similarity_threshold=0.95)
        assert result.duplicates_removed == 0
        assert result.total_scanned == 2

    @pytest.mark.asyncio
    async def test_empty_bank(self):
        vs = InMemoryVectorStore()
        result = await run_consolidation(vs, "empty", similarity_threshold=0.95)
        assert result.total_scanned == 0
        assert result.duplicates_removed == 0

    @pytest.mark.asyncio
    async def test_stale_archival(self):
        vs = InMemoryVectorStore()
        old_date = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        await vs.store_vectors([
            VectorItem(
                id="v1", text="old", vector=[1.0, 0.0], bank_id="b1",
                metadata={"_created_at": old_date},
            ),
            VectorItem(
                id="v2", text="new", vector=[0.0, 1.0], bank_id="b1",
                metadata={"_created_at": datetime.now(timezone.utc).isoformat()},
            ),
        ])
        result = await run_consolidation(
            vs, "b1", similarity_threshold=0.99,
            archive_unretrieved_after_days=30,
        )
        assert result.stale_archived == 1
        # v2 should still exist
        remaining = await vs.list_vectors("b1")
        assert len(remaining) == 1
        assert remaining[0].id == "v2"

    @pytest.mark.asyncio
    async def test_no_list_vectors_support(self):
        """VectorStore without list_vectors should return empty result."""

        class MinimalStore:
            async def search_similar(self, *a, **kw):
                return []

        result = await run_consolidation(MinimalStore(), "b1")
        assert result.total_scanned == 0
