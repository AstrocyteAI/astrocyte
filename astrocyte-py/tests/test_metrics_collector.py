"""Unit tests for ``BenchmarkMetricsCollector``.

The collector is observability-only: it must never raise from a recording
call (production hot paths absorb any error), it must produce a clean
``EvalMetrics`` summary on finalize, and it must be silent / idempotent
on no-op cases (empty record stream).
"""

from __future__ import annotations

import pytest

from astrocyte.eval.metrics_collector import (
    BenchmarkMetricsCollector,
    _bucket_label,
    _estimate_cost_usd,
    _percentile,
)
from astrocyte.types import EvalMetrics

# ---------------------------------------------------------------------------
# Helpers used by tests
# ---------------------------------------------------------------------------


def _empty_base() -> EvalMetrics:
    return EvalMetrics(
        recall_precision=0.0,
        recall_hit_rate=0.0,
        recall_mrr=0.0,
        recall_ndcg=0.0,
        retain_latency_p50_ms=0.0,
        retain_latency_p95_ms=0.0,
        recall_latency_p50_ms=0.0,
        recall_latency_p95_ms=0.0,
        total_tokens_used=0,
        total_duration_seconds=0.0,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestPercentile:
    def test_empty_returns_none(self):
        assert _percentile([], 50) is None

    def test_single_value(self):
        assert _percentile([42.0], 50) == 42.0
        assert _percentile([42.0], 99) == 42.0

    def test_p50_p95(self):
        # 11 evenly-spaced values 0..100
        values = [float(i * 10) for i in range(11)]
        assert _percentile(values, 50) == pytest.approx(50.0)
        assert _percentile(values, 95) == pytest.approx(95.0)


class TestBucketLabel:
    def test_zero(self):
        assert _bucket_label(0.0) == "0.0-0.1"

    def test_max(self):
        assert _bucket_label(1.0) == "0.9-1.0"

    def test_middle(self):
        assert _bucket_label(0.55) == "0.5-0.6"

    def test_clamps_negative(self):
        assert _bucket_label(-0.5) == "0.0-0.1"

    def test_clamps_over_one(self):
        assert _bucket_label(1.5) == "0.9-1.0"


class TestEstimateCost:
    def test_known_model(self):
        # gpt-4o-mini: $0.15 / $0.60 per 1M
        cost = _estimate_cost_usd("gpt-4o-mini", input_tokens=1_000_000, output_tokens=0)
        assert cost == pytest.approx(0.15, abs=1e-6)

    def test_unknown_model_returns_none(self):
        assert _estimate_cost_usd("not-a-real-model", 1000, 1000) is None

    def test_none_model(self):
        assert _estimate_cost_usd(None, 1, 1) is None

    def test_embedding_no_output(self):
        # text-embedding-3-small: $0.02 input, $0.0 output
        cost = _estimate_cost_usd("text-embedding-3-small", input_tokens=1_000_000, output_tokens=0)
        assert cost == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# Collector behaviour
# ---------------------------------------------------------------------------


class TestEmptyCollector:
    def test_finalize_with_no_records_is_safe(self):
        c = BenchmarkMetricsCollector()
        out = c.finalize(_empty_base())
        # Required fields preserved
        assert out.recall_precision == 0.0
        # Optional fields stay None when no signal recorded
        assert out.recall_coverage is None
        assert out.recall_reflect_gap is None
        assert out.cascade_decisions is None
        assert out.cost_total_usd is None
        assert out.tokens_by_phase is None


class TestPhaseTokens:
    def test_tokens_bucketed_by_phase(self):
        c = BenchmarkMetricsCollector()
        c.set_phase("retain")
        c.record_completion_call(model="gpt-4o-mini", input_tokens=100, output_tokens=50)
        c.record_embedding_call(model="text-embedding-3-small", tokens=200)

        c.set_phase("eval")
        c.record_completion_call(model="gpt-4o-mini", input_tokens=300, output_tokens=80)

        out = c.finalize(_empty_base())
        assert out.tokens_by_phase == {
            "retain": 350,   # 100+50+200
            "eval": 380,     # 300+80
        }
        assert out.api_calls_total == 3
        assert out.api_calls_by_endpoint == {"chat/completions": 2, "embeddings": 1}
        assert out.cost_total_usd is not None
        assert out.cost_total_usd > 0


class TestRecallReflectGap:
    def test_cross_tab_buckets(self):
        c = BenchmarkMetricsCollector()
        # 4 questions covering all four cells
        c.record_question(idx=0, recall_hit=True, reflect_hit=True, correct=True)
        c.record_question(idx=1, recall_hit=True, reflect_hit=False, correct=False)
        c.record_question(idx=2, recall_hit=False, reflect_hit=True, correct=True)
        c.record_question(idx=3, recall_hit=False, reflect_hit=False, correct=False)

        out = c.finalize(_empty_base())
        assert out.recall_reflect_gap == {
            "both_hit": 1,
            "recall_hit_reflect_miss": 1,
            "recall_miss_reflect_hit": 1,
            "both_miss": 1,
        }
        # recall_coverage = (both_hit + recall_only) / total = 2 / 4 = 0.5
        assert out.recall_coverage == pytest.approx(0.5)


class TestAdversarialAbstention:
    def test_abstention_rate_only_for_adversarial(self):
        c = BenchmarkMetricsCollector()
        c.record_question(idx=0, category="adversarial", abstained=True)
        c.record_question(idx=1, category="adversarial", abstained=True)
        c.record_question(idx=2, category="adversarial", abstained=False)
        c.record_question(idx=3, category="single-hop", abstained=False)

        out = c.finalize(_empty_base())
        # 2 of 3 adversarial abstained → 0.667
        assert out.abstention_rate_adversarial == pytest.approx(2 / 3)

    def test_no_adversarial_means_none(self):
        c = BenchmarkMetricsCollector()
        c.record_question(idx=0, category="single-hop")
        out = c.finalize(_empty_base())
        assert out.abstention_rate_adversarial is None


class TestCascadeDecisions:
    def test_decision_counts_and_score_histogram(self):
        c = BenchmarkMetricsCollector()
        c.record_cascade_decision("trigram_autolink")
        c.record_cascade_decision("trigram_autolink")
        c.record_cascade_decision("composite_autolink", composite_score=0.65)
        c.record_cascade_decision("composite_autolink", composite_score=0.72)
        c.record_cascade_decision("skipped", composite_score=0.45)

        out = c.finalize(_empty_base())
        assert out.cascade_decisions == {
            "trigram_autolink": 2,
            "composite_autolink": 2,
            "skipped": 1,
        }
        # Composite scores recorded → histogram populated
        assert out.composite_score_distribution is not None
        # 0.45 → bucket "0.4-0.5", 0.65 → "0.6-0.7", 0.72 → "0.7-0.8"
        assert out.composite_score_distribution["0.4-0.5"] == 1
        assert out.composite_score_distribution["0.6-0.7"] == 1
        assert out.composite_score_distribution["0.7-0.8"] == 1


class TestCanonicalResolution:
    def test_resolved_vs_created_counts(self):
        c = BenchmarkMetricsCollector()
        c.record_canonical_resolution(resolved=True)
        c.record_canonical_resolution(resolved=True)
        c.record_canonical_resolution(resolved=False)

        out = c.finalize(_empty_base())
        assert out.entities_resolved_count == 2
        assert out.entities_created_count == 1


class TestErrorAndRetry:
    def test_error_categories(self):
        c = BenchmarkMetricsCollector()
        c.record_error("pool_timeout")
        c.record_error("pool_timeout")
        c.record_error("deadlock")
        c.record_openai_retry()
        c.record_failed_retain()
        c.record_failed_retain()

        out = c.finalize(_empty_base())
        assert out.error_count_by_type == {"pool_timeout": 2, "deadlock": 1}
        assert out.openai_retry_count == 1
        assert out.failed_retain_count == 2


class TestLatencyTail:
    def test_p99_and_e2e(self):
        c = BenchmarkMetricsCollector()
        # Retain latencies
        for ms in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100):
            c.record_retain_latency(ms)
        # Recall + reflect for one question → e2e p50
        c.record_recall_latency(50.0)
        c.record_reflect_latency(150.0)
        c.record_question(idx=0, recall_latency_ms=50.0, reflect_latency_ms=150.0)

        out = c.finalize(_empty_base())
        assert out.retain_latency_p99_ms == pytest.approx(99.1, abs=1)
        assert out.recall_latency_p99_ms == pytest.approx(50.0)
        assert out.reflect_latency_p99_ms == pytest.approx(150.0)
        # e2e = 50 + 150 = 200
        assert out.e2e_per_question_p50_ms == pytest.approx(200.0)


class TestWikiPageStats:
    def test_pages_per_persona(self):
        c = BenchmarkMetricsCollector()
        c.set_wiki_page_stats(total=50, unique_personas=10)
        out = c.finalize(_empty_base())
        assert out.wiki_pages_total == 50
        assert out.wiki_pages_per_persona == 5.0

    def test_zero_personas_safe(self):
        c = BenchmarkMetricsCollector()
        c.set_wiki_page_stats(total=0, unique_personas=0)
        out = c.finalize(_empty_base())
        assert out.wiki_pages_total == 0
        assert out.wiki_pages_per_persona == 0.0
