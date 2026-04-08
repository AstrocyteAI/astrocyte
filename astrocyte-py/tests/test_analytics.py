"""Tests for bank health analytics."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from astrocyte.analytics import BankMetricsCollector, compute_bank_health, counters_to_quality_point


# ---------------------------------------------------------------------------
# BankMetricsCollector
# ---------------------------------------------------------------------------


class TestBankMetricsCollector:
    def test_record_retain(self) -> None:
        c = BankMetricsCollector()
        c.record_retain("b1", 100)
        c.record_retain("b1", 200, deduplicated=True)
        counters = c.get_counters("b1")
        assert counters.retain_count == 2
        assert counters.total_content_length == 300
        assert counters.dedup_count == 1

    def test_record_recall(self) -> None:
        c = BankMetricsCollector()
        c.record_recall("b1", hit_count=3, top_score=0.85)
        c.record_recall("b1", hit_count=0, top_score=0.0)
        counters = c.get_counters("b1")
        assert counters.recall_count == 2
        assert counters.recall_hits == 1
        assert counters.total_recall_score == pytest.approx(0.85)

    def test_record_reflect(self) -> None:
        c = BankMetricsCollector()
        c.record_reflect("b1", success=True)
        c.record_reflect("b1", success=False)
        counters = c.get_counters("b1")
        assert counters.reflect_count == 2
        assert counters.reflect_success == 1

    def test_bank_ids(self) -> None:
        c = BankMetricsCollector()
        c.record_retain("a", 10)
        c.record_retain("b", 20)
        assert set(c.bank_ids()) == {"a", "b"}

    def test_reset_single_bank(self) -> None:
        c = BankMetricsCollector()
        c.record_retain("a", 10)
        c.record_retain("b", 20)
        c.reset("a")
        assert "a" not in c.bank_ids()
        assert "b" in c.bank_ids()

    def test_reset_all(self) -> None:
        c = BankMetricsCollector()
        c.record_retain("a", 10)
        c.record_retain("b", 20)
        c.reset()
        assert c.bank_ids() == []


# ---------------------------------------------------------------------------
# Health scoring
# ---------------------------------------------------------------------------


class TestComputeBankHealth:
    def test_no_operations_returns_neutral(self) -> None:
        c = BankMetricsCollector()
        health = compute_bank_health("b1", c.get_counters("b1"))
        assert health.score == 0.5
        assert health.status == "warning"
        assert health.issues == []

    def test_healthy_bank(self) -> None:
        c = BankMetricsCollector()
        for _ in range(20):
            c.record_retain("b1", 150)
        for _ in range(15):
            c.record_recall("b1", hit_count=3, top_score=0.85)
        for _ in range(5):
            c.record_reflect("b1", success=True)
        health = compute_bank_health("b1", c.get_counters("b1"))
        assert health.score >= 0.7
        assert health.status == "healthy"

    def test_unhealthy_bank_low_hit_rate(self) -> None:
        c = BankMetricsCollector()
        for _ in range(10):
            c.record_retain("b1", 50)
        for _ in range(10):
            c.record_recall("b1", hit_count=0, top_score=0.0)
        health = compute_bank_health("b1", c.get_counters("b1"))
        assert health.score < 0.7
        codes = [i.code for i in health.issues]
        assert "LOW_RECALL_HIT_RATE" in codes

    def test_high_dedup_rate_warning(self) -> None:
        c = BankMetricsCollector()
        for _ in range(10):
            c.record_retain("b1", 100, deduplicated=True)
        for _ in range(5):
            c.record_retain("b1", 100)
        health = compute_bank_health("b1", c.get_counters("b1"))
        codes = [i.code for i in health.issues]
        assert "HIGH_DEDUP_RATE" in codes

    def test_short_content_info(self) -> None:
        c = BankMetricsCollector()
        for _ in range(10):
            c.record_retain("b1", 5)
        health = compute_bank_health("b1", c.get_counters("b1"))
        codes = [i.code for i in health.issues]
        assert "SHORT_CONTENT" in codes

    def test_metrics_dict_populated(self) -> None:
        c = BankMetricsCollector()
        c.record_retain("b1", 100)
        c.record_recall("b1", 2, 0.8)
        health = compute_bank_health("b1", c.get_counters("b1"))
        assert "recall_hit_rate" in health.metrics
        assert "avg_recall_score" in health.metrics
        assert "dedup_rate" in health.metrics
        assert "retain_count" in health.metrics

    def test_memory_count_in_metrics(self) -> None:
        c = BankMetricsCollector()
        c.record_retain("b1", 100)
        health = compute_bank_health("b1", c.get_counters("b1"), memory_count=42)
        assert health.metrics["memory_count"] == 42.0


# ---------------------------------------------------------------------------
# Quality data point
# ---------------------------------------------------------------------------


class TestQualityDataPoint:
    def test_snapshot_from_counters(self) -> None:
        c = BankMetricsCollector()
        c.record_retain("b1", 100)
        c.record_retain("b1", 200, deduplicated=True)
        c.record_recall("b1", 3, 0.9)
        c.record_recall("b1", 0, 0.0)
        c.record_reflect("b1", success=True)
        dp = counters_to_quality_point(c.get_counters("b1"))
        assert dp.date == date.today()
        assert dp.retain_count == 2
        assert dp.recall_count == 2
        assert dp.recall_hit_rate == 0.5
        assert dp.dedup_rate == 0.5
        assert dp.reflect_success_rate == 1.0


# ---------------------------------------------------------------------------
# Integration with Astrocyte
# ---------------------------------------------------------------------------


class TestAstrocyteAnalyticsIntegration:
    """Test that Astrocyte records analytics during operations."""

    def _make_brain(self):
        from astrocyte._astrocyte import Astrocyte
        from astrocyte.config import AstrocyteConfig
        from astrocyte.pipeline.orchestrator import PipelineOrchestrator
        from astrocyte.testing.in_memory import InMemoryVectorStore
        from astrocyte.testing.in_memory import MockLLMProvider

        config = AstrocyteConfig()
        config.provider_tier = "storage"
        config.barriers.pii.mode = "disabled"
        brain = Astrocyte(config)
        vs = InMemoryVectorStore()
        pipeline = PipelineOrchestrator(vector_store=vs, llm_provider=MockLLMProvider())
        brain.set_pipeline(pipeline)
        return brain

    def test_retain_recorded(self) -> None:
        brain = self._make_brain()
        asyncio.run(brain.retain("Test content", bank_id="b1"))
        counters = brain._analytics.get_counters("b1")
        assert counters.retain_count == 1
        assert counters.total_content_length == len("Test content")

    def test_recall_recorded(self) -> None:
        brain = self._make_brain()
        asyncio.run(brain.retain("Python is great", bank_id="b1"))
        asyncio.run(brain.recall("Python", bank_id="b1"))
        counters = brain._analytics.get_counters("b1")
        assert counters.recall_count == 1

    def test_reflect_recorded(self) -> None:
        brain = self._make_brain()
        asyncio.run(brain.retain("Dark mode is preferred", bank_id="b1"))
        asyncio.run(brain.reflect("preferences", bank_id="b1"))
        counters = brain._analytics.get_counters("b1")
        assert counters.reflect_count == 1

    def test_bank_health_api(self) -> None:
        brain = self._make_brain()
        asyncio.run(brain.retain("Some content", bank_id="b1"))
        asyncio.run(brain.recall("query", bank_id="b1"))
        health = asyncio.run(brain.bank_health("b1"))
        assert health.bank_id == "b1"
        assert 0.0 <= health.score <= 1.0
        assert health.status in ("healthy", "warning", "unhealthy")

    def test_all_bank_health_api(self) -> None:
        brain = self._make_brain()
        asyncio.run(brain.retain("a", bank_id="b1"))
        asyncio.run(brain.retain("b", bank_id="b2"))
        healths = asyncio.run(brain.all_bank_health())
        assert len(healths) == 2
        assert {h.bank_id for h in healths} == {"b1", "b2"}

    def test_quality_snapshot_api(self) -> None:
        brain = self._make_brain()
        asyncio.run(brain.retain("content", bank_id="b1"))
        dp = brain.bank_quality_snapshot("b1")
        assert dp.retain_count == 1
        assert dp.date == date.today()
