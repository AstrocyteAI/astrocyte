"""Tests for policy/signal_quality.py — cosine similarity, dedup detection."""

import math

import pytest

from astrocyte.policy.signal_quality import DedupDetector, cosine_similarity


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


class TestDedupDetectorThresholdOverride:
    """Per-call threshold override for MIP DedupSpec.threshold (Phase 1, Step 5)."""

    def _near_but_not_identical(self) -> tuple[list[float], list[float]]:
        # Two vectors with cosine similarity ~0.93 — between common thresholds
        a = [1.0, 0.0, 0.0]
        b = [0.93, 0.36764, 0.0]  # tuned to land around 0.93
        return a, b

    def test_override_below_default_finds_duplicate(self):
        """Default threshold 0.95 misses; override 0.90 catches."""
        detector = DedupDetector(similarity_threshold=0.95)
        a, b = self._near_but_not_identical()
        detector.add("bank-1", "m1", a)

        # Without override: not a duplicate at 0.95
        is_dup_default, sim = detector.is_duplicate("bank-1", b)
        assert is_dup_default is False
        assert 0.90 < sim < 0.95

        # With override: is a duplicate at 0.90
        is_dup_override, _ = detector.is_duplicate("bank-1", b, threshold_override=0.90)
        assert is_dup_override is True

    def test_override_above_default_misses_duplicate(self):
        """Default threshold 0.90 catches; override 0.99 misses near-but-not-identical."""
        detector = DedupDetector(similarity_threshold=0.90)
        a, b = self._near_but_not_identical()
        detector.add("bank-1", "m1", a)

        is_dup_default, _ = detector.is_duplicate("bank-1", b)
        assert is_dup_default is True

        is_dup_override, _ = detector.is_duplicate("bank-1", b, threshold_override=0.99)
        assert is_dup_override is False

    def test_override_none_uses_instance_default(self):
        detector = DedupDetector(similarity_threshold=0.95)
        v = [1.0, 2.0, 3.0]
        detector.add("bank-1", "m1", v)
        is_dup, _ = detector.is_duplicate("bank-1", v, threshold_override=None)
        assert is_dup is True

    def test_override_does_not_mutate_instance_threshold(self):
        detector = DedupDetector(similarity_threshold=0.95)
        a, b = self._near_but_not_identical()
        detector.add("bank-1", "m1", a)

        detector.is_duplicate("bank-1", b, threshold_override=0.50)
        assert detector.threshold == 0.95  # instance default unchanged

        # Subsequent call without override still uses 0.95
        is_dup, _ = detector.is_duplicate("bank-1", b)
        assert is_dup is False
