"""Tests for astrocyte.eval — MemoryEvaluator, suites, metrics, regressions."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.eval.evaluator import MemoryEvaluator, detect_regressions, format_comparison
from astrocyte.eval.metrics import (
    ndcg_at_k,
    percentile,
    precision_at_k,
    recall_hit,
    reciprocal_rank,
    text_overlap_score,
)
from astrocyte.eval.suite import EvalSuite, RecallCase, ReflectCase, RetainCase, load_suite
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryEngineProvider, InMemoryVectorStore, MockLLMProvider
from astrocyte.types import EvalMetrics, EvalResult, MemoryHit, QueryResult


def _make_brain() -> tuple[Astrocyte, InMemoryEngineProvider]:
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    brain.set_engine_provider(engine)
    return brain, engine


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    def test_precision_at_k(self):
        relevant = {"a", "b"}
        retrieved = ["a", "c", "b", "d"]
        assert precision_at_k(relevant, retrieved) == 0.5

    def test_precision_empty(self):
        assert precision_at_k({"a"}, []) == 0.0

    def test_recall_hit_true(self):
        assert recall_hit({"a"}, ["b", "a"]) is True

    def test_recall_hit_false(self):
        assert recall_hit({"a"}, ["b", "c"]) is False

    def test_reciprocal_rank_first(self):
        assert reciprocal_rank({"a"}, ["a", "b"]) == 1.0

    def test_reciprocal_rank_second(self):
        assert reciprocal_rank({"a"}, ["b", "a"]) == 0.5

    def test_reciprocal_rank_none(self):
        assert reciprocal_rank({"a"}, ["b", "c"]) == 0.0

    def test_ndcg_perfect(self):
        assert ndcg_at_k({"a", "b"}, ["a", "b"]) == 1.0

    def test_ndcg_imperfect(self):
        score = ndcg_at_k({"a"}, ["b", "a"])
        assert 0.0 < score < 1.0

    def test_ndcg_empty(self):
        assert ndcg_at_k(set(), ["a"]) == 0.0

    def test_percentile_median(self):
        assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0

    def test_percentile_empty(self):
        assert percentile([], 50) == 0.0

    def test_text_overlap_all_found(self):
        assert text_overlap_score(["dark", "mode"], "Calvin prefers dark mode") == 1.0

    def test_text_overlap_partial(self):
        assert text_overlap_score(["dark", "blue"], "Calvin prefers dark mode") == 0.5

    def test_text_overlap_none(self):
        assert text_overlap_score(["blue", "green"], "Calvin prefers dark mode") == 0.0

    def test_text_overlap_no_expectations(self):
        assert text_overlap_score([], "anything") == 1.0


# ---------------------------------------------------------------------------
# Suite loading
# ---------------------------------------------------------------------------


class TestSuiteLoading:
    def test_load_basic(self):
        suite = load_suite("basic")
        assert suite.name == "basic"
        assert len(suite.retains) >= 5
        assert len(suite.recalls) >= 5
        assert len(suite.reflects) >= 1

    def test_load_accuracy(self):
        suite = load_suite("accuracy")
        assert suite.name == "accuracy"
        assert len(suite.retains) >= 10
        assert len(suite.recalls) >= 10

    def test_unknown_suite_raises(self):
        with pytest.raises(ValueError, match="Unknown suite"):
            load_suite("nonexistent")

    def test_load_yaml_suite(self, tmp_path: Path):
        yaml_file = tmp_path / "custom.yaml"
        yaml_file.write_text("""
name: "My custom suite"
retain:
  - content: "Test memory one"
    tags: [test]
  - content: "Test memory two"
recall:
  - query: "test"
    expected_contains: ["memory"]
reflect:
  - query: "summarize"
    expected_topics: ["memory"]
""")
        suite = load_suite(str(yaml_file))
        assert suite.name == "My custom suite"
        assert len(suite.retains) == 2
        assert len(suite.recalls) == 1
        assert len(suite.reflects) == 1


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


class TestMemoryEvaluator:
    async def test_run_basic_suite(self):
        brain, _ = _make_brain()
        evaluator = MemoryEvaluator(brain)
        result = await evaluator.run_suite("basic", bank_id="eval-test")

        assert result.suite == "basic"
        assert result.provider == "test"
        assert 0.0 <= result.metrics.recall_precision <= 1.0
        assert 0.0 <= result.metrics.recall_hit_rate <= 1.0
        assert 0.0 <= result.metrics.recall_mrr <= 1.0
        assert result.metrics.retain_latency_p50_ms > 0.0
        assert result.metrics.total_duration_seconds > 0.0
        assert len(result.per_query_results) >= 5

    async def test_run_accuracy_suite(self):
        brain, _ = _make_brain()
        evaluator = MemoryEvaluator(brain)
        result = await evaluator.run_suite("accuracy", bank_id="eval-accuracy")

        assert result.suite == "accuracy"
        assert len(result.per_query_results) >= 10

    async def test_run_custom_suite(self):
        brain, _ = _make_brain()
        evaluator = MemoryEvaluator(brain)

        custom = EvalSuite(
            name="mini",
            retains=[RetainCase(content="Python is great")],
            recalls=[RecallCase(query="Python", expected_contains=["Python"])],
        )
        result = await evaluator.run_suite(custom, bank_id="eval-mini")

        assert result.suite == "mini"
        assert result.metrics.recall_hit_rate > 0.0

    async def test_clean_after(self):
        brain, engine = _make_brain()
        evaluator = MemoryEvaluator(brain)

        custom = EvalSuite(
            name="cleanup-test",
            retains=[RetainCase(content="temporary data")],
            recalls=[RecallCase(query="temporary", expected_contains=["temporary"])],
        )
        await evaluator.run_suite(custom, bank_id="eval-cleanup", clean_after=True)

        # Bank should be cleaned
        assert len(engine._memories.get("eval-cleanup", [])) == 0

    async def test_no_clean_after(self):
        brain, engine = _make_brain()
        evaluator = MemoryEvaluator(brain)

        custom = EvalSuite(
            name="keep-test",
            retains=[RetainCase(content="persistent data")],
            recalls=[RecallCase(query="persistent", expected_contains=["persistent"])],
        )
        await evaluator.run_suite(custom, bank_id="eval-keep", clean_after=False)

        # Bank should still have data
        assert len(engine._memories.get("eval-keep", [])) >= 1

    async def test_reflect_evaluation(self):
        brain, _ = _make_brain()
        evaluator = MemoryEvaluator(brain)

        custom = EvalSuite(
            name="reflect-test",
            retains=[
                RetainCase(content="Calvin likes dark mode and Python"),
            ],
            recalls=[],
            reflects=[
                ReflectCase(query="What does Calvin like?", expected_topics=["dark mode", "Python"]),
            ],
        )
        result = await evaluator.run_suite(custom, bank_id="eval-reflect")
        assert result.metrics.reflect_accuracy is not None

    async def test_per_query_results(self):
        brain, _ = _make_brain()
        evaluator = MemoryEvaluator(brain)

        custom = EvalSuite(
            name="per-query",
            retains=[RetainCase(content="Dark mode is preferred")],
            recalls=[RecallCase(query="dark mode", expected_contains=["dark"])],
        )
        result = await evaluator.run_suite(custom, bank_id="eval-pq")

        assert len(result.per_query_results) == 1
        qr = result.per_query_results[0]
        assert qr.query == "dark mode"
        assert qr.latency_ms > 0


# ---------------------------------------------------------------------------
# Regression detection
# ---------------------------------------------------------------------------


class TestRegressionDetection:
    def test_no_regression(self):
        baseline = EvalMetrics(
            recall_precision=0.8,
            recall_hit_rate=0.9,
            recall_mrr=0.7,
            recall_ndcg=0.75,
            retain_latency_p50_ms=10.0,
            retain_latency_p95_ms=50.0,
            recall_latency_p50_ms=20.0,
            recall_latency_p95_ms=100.0,
            total_tokens_used=1000,
            total_duration_seconds=30.0,
        )
        current = EvalMetrics(
            recall_precision=0.82,
            recall_hit_rate=0.91,
            recall_mrr=0.72,
            recall_ndcg=0.76,
            retain_latency_p50_ms=9.0,
            retain_latency_p95_ms=48.0,
            recall_latency_p50_ms=18.0,
            recall_latency_p95_ms=95.0,
            total_tokens_used=1000,
            total_duration_seconds=28.0,
        )
        alerts = detect_regressions(current, baseline, threshold=0.05)
        assert alerts == []

    def test_regression_detected(self):
        baseline = EvalMetrics(
            recall_precision=0.8,
            recall_hit_rate=0.9,
            recall_mrr=0.7,
            recall_ndcg=0.75,
            retain_latency_p50_ms=10.0,
            retain_latency_p95_ms=50.0,
            recall_latency_p50_ms=20.0,
            recall_latency_p95_ms=100.0,
            total_tokens_used=1000,
            total_duration_seconds=30.0,
        )
        current = EvalMetrics(
            recall_precision=0.5,
            recall_hit_rate=0.6,
            recall_mrr=0.4,
            recall_ndcg=0.45,
            retain_latency_p50_ms=10.0,
            retain_latency_p95_ms=50.0,
            recall_latency_p50_ms=20.0,
            recall_latency_p95_ms=100.0,
            total_tokens_used=1000,
            total_duration_seconds=30.0,
        )
        alerts = detect_regressions(current, baseline, threshold=0.05)
        assert len(alerts) >= 1
        assert any(a.metric == "recall_precision" for a in alerts)
        assert any(a.severity in ("warning", "critical") for a in alerts)

    def test_critical_regression(self):
        baseline = EvalMetrics(
            recall_precision=0.8,
            recall_hit_rate=0.9,
            recall_mrr=0.7,
            recall_ndcg=0.75,
            retain_latency_p50_ms=10.0,
            retain_latency_p95_ms=50.0,
            recall_latency_p50_ms=20.0,
            recall_latency_p95_ms=100.0,
            total_tokens_used=1000,
            total_duration_seconds=30.0,
        )
        current = EvalMetrics(
            recall_precision=0.1,
            recall_hit_rate=0.1,
            recall_mrr=0.1,
            recall_ndcg=0.1,
            retain_latency_p50_ms=10.0,
            retain_latency_p95_ms=50.0,
            recall_latency_p50_ms=20.0,
            recall_latency_p95_ms=100.0,
            total_tokens_used=1000,
            total_duration_seconds=30.0,
        )
        alerts = detect_regressions(current, baseline, threshold=0.05)
        assert any(a.severity == "critical" for a in alerts)


# ---------------------------------------------------------------------------
# Comparison formatting
# ---------------------------------------------------------------------------


class TestFormatComparison:
    async def test_format_two_results(self):
        brain, _ = _make_brain()
        evaluator = MemoryEvaluator(brain)

        custom = EvalSuite(
            name="mini",
            retains=[RetainCase(content="Test data")],
            recalls=[RecallCase(query="Test", expected_contains=["Test"])],
        )

        r1 = await evaluator.run_suite(custom, bank_id="cmp-1", clean_after=True)
        r2 = await evaluator.run_suite(custom, bank_id="cmp-2", clean_after=True)

        table = format_comparison([r1, r2])
        assert "Recall precision" in table
        assert "Recall MRR" in table
        assert len(table.split("\n")) >= 5

    def test_format_empty(self):
        assert "No results" in format_comparison([])


# ---------------------------------------------------------------------------
# Token tracking
# ---------------------------------------------------------------------------


def _make_tier1_brain() -> tuple[Astrocyte, MockLLMProvider]:
    """Create a Tier 1 Astrocyte with pipeline (for token tracking tests)."""
    config = AstrocyteConfig()
    config.provider_tier = "storage"
    config.barriers.pii.mode = "disabled"
    config.escalation.degraded_mode = "error"

    brain = Astrocyte(config)
    vector_store = InMemoryVectorStore()
    llm = MockLLMProvider()
    pipeline = PipelineOrchestrator(vector_store=vector_store, llm_provider=llm)
    brain.set_pipeline(pipeline)
    return brain, llm


class TestTokenTracking:
    def test_pipeline_tracks_tokens(self):
        """Token counter accumulates usage from complete() calls."""
        vector_store = InMemoryVectorStore()
        llm = MockLLMProvider()
        pipeline = PipelineOrchestrator(vector_store=vector_store, llm_provider=llm)

        assert pipeline.tokens_used == 0

    async def test_retain_accumulates_tokens(self):
        """A retain call goes through embed + entity extraction, accumulating tokens."""
        brain, llm = _make_tier1_brain()
        assert brain._pipeline is not None
        assert brain._pipeline.tokens_used == 0

        await brain.retain("Calvin prefers dark mode", bank_id="tok-test")

        # MockLLMProvider returns 30 tokens per complete() call (10 in + 20 out).
        # Retain calls: entity extraction (1 complete). Embed uses embed() not complete().
        assert brain._pipeline.tokens_used > 0

    async def test_reset_token_counter(self):
        """reset_token_counter returns accumulated tokens and resets to zero."""
        brain, _ = _make_tier1_brain()
        assert brain._pipeline is not None

        await brain.retain("Test content", bank_id="tok-reset")
        tokens_before = brain._pipeline.tokens_used
        assert tokens_before > 0

        returned = brain._pipeline.reset_token_counter()
        assert returned == tokens_before
        assert brain._pipeline.tokens_used == 0

    async def test_eval_reports_tokens(self):
        """Evaluator reports non-zero total_tokens_used for Tier 1 pipeline."""
        brain, _ = _make_tier1_brain()
        evaluator = MemoryEvaluator(brain)

        custom = EvalSuite(
            name="token-test",
            retains=[RetainCase(content="Dark mode is preferred")],
            recalls=[RecallCase(query="dark mode", expected_contains=["dark"])],
        )
        result = await evaluator.run_suite(custom, bank_id="eval-tok")

        # Tier 1 pipeline makes LLM calls for entity extraction during retain/recall
        assert result.metrics.total_tokens_used > 0


# ---------------------------------------------------------------------------
# EvalResult serialization
# ---------------------------------------------------------------------------


class TestEvalResultSerialization:
    def _make_result(self) -> EvalResult:
        return EvalResult(
            suite="test",
            provider="mock",
            provider_tier="storage",
            timestamp=datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc),
            metrics=EvalMetrics(
                recall_precision=0.8,
                recall_hit_rate=0.9,
                recall_mrr=0.7,
                recall_ndcg=0.75,
                retain_latency_p50_ms=10.0,
                retain_latency_p95_ms=50.0,
                recall_latency_p50_ms=20.0,
                recall_latency_p95_ms=100.0,
                total_tokens_used=500,
                total_duration_seconds=5.0,
                reflect_accuracy=0.85,
            ),
            per_query_results=[
                QueryResult(
                    query="test query",
                    expected=["answer"],
                    actual=[MemoryHit(text="answer text", score=0.9, bank_id="b")],
                    relevant_found=1,
                    precision=1.0,
                    reciprocal_rank=1.0,
                    latency_ms=15.0,
                ),
            ],
            config_snapshot={"judge": "keyword"},
        )

    def test_to_dict_returns_dict(self):
        result = self._make_result()
        d = result.to_dict()
        assert isinstance(d, dict)
        assert d["suite"] == "test"
        assert d["provider"] == "mock"
        assert d["metrics"]["recall_precision"] == 0.8
        assert d["metrics"]["total_tokens_used"] == 500
        assert len(d["per_query_results"]) == 1

    def test_to_dict_datetime_is_string(self):
        result = self._make_result()
        d = result.to_dict()
        assert isinstance(d["timestamp"], str)
        assert "2026-04-08" in d["timestamp"]

    def test_to_json_is_valid_json(self):
        result = self._make_result()
        j = result.to_json()
        parsed = json.loads(j)
        assert parsed["suite"] == "test"
        assert parsed["metrics"]["total_tokens_used"] == 500

    def test_to_json_roundtrip(self):
        result = self._make_result()
        j = result.to_json()
        parsed = json.loads(j)
        # Verify all key fields survive roundtrip
        assert parsed["provider_tier"] == "storage"
        assert parsed["metrics"]["reflect_accuracy"] == 0.85
        assert parsed["per_query_results"][0]["query"] == "test query"
        assert parsed["config_snapshot"]["judge"] == "keyword"
