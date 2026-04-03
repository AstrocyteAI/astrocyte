"""Tests for policy/signal_quality.py — cosine similarity, dedup detection."""

import math

import pytest

from astrocytes.policy.signal_quality import DedupDetector, cosine_similarity


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 2.0, 3.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_zero_vector(self):
        a = [0.0, 0.0]
        b = [1.0, 2.0]
        assert cosine_similarity(a, b) == 0.0

    def test_dimension_mismatch(self):
        with pytest.raises(ValueError, match="mismatch"):
            cosine_similarity([1.0], [1.0, 2.0])

    def test_unit_vectors(self):
        a = [1.0 / math.sqrt(2), 1.0 / math.sqrt(2)]
        b = [1.0, 0.0]
        # cos(45°) ≈ 0.707
        assert cosine_similarity(a, b) == pytest.approx(1.0 / math.sqrt(2), abs=1e-6)


class TestDedupDetector:
    def test_not_duplicate(self):
        detector = DedupDetector(similarity_threshold=0.95)
        detector.add("bank-1", "m1", [1.0, 0.0, 0.0])
        is_dup, sim = detector.is_duplicate("bank-1", [0.0, 1.0, 0.0])
        assert is_dup is False

    def test_is_duplicate(self):
        detector = DedupDetector(similarity_threshold=0.95)
        v = [1.0, 2.0, 3.0]
        detector.add("bank-1", "m1", v)
        is_dup, sim = detector.is_duplicate("bank-1", v)
        assert is_dup is True
        assert sim >= 0.95

    def test_bank_isolation(self):
        detector = DedupDetector(similarity_threshold=0.95)
        v = [1.0, 2.0, 3.0]
        detector.add("bank-1", "m1", v)
        is_dup, sim = detector.is_duplicate("bank-2", v)
        assert is_dup is False  # Different bank

    def test_cache_eviction(self):
        detector = DedupDetector(similarity_threshold=0.95, max_cache_per_bank=2)
        detector.add("bank-1", "m1", [1.0, 0.0])
        detector.add("bank-1", "m2", [0.0, 1.0])
        detector.add("bank-1", "m3", [0.5, 0.5])
        # m1 should have been evicted
        is_dup, _ = detector.is_duplicate("bank-1", [1.0, 0.0])
        assert is_dup is False  # m1 was evicted

    def test_clear_bank(self):
        detector = DedupDetector()
        detector.add("bank-1", "m1", [1.0, 2.0, 3.0])
        detector.clear_bank("bank-1")
        is_dup, _ = detector.is_duplicate("bank-1", [1.0, 2.0, 3.0])
        assert is_dup is False

    def test_empty_bank(self):
        detector = DedupDetector()
        is_dup, sim = detector.is_duplicate("bank-1", [1.0, 2.0])
        assert is_dup is False
        assert sim == 0.0
