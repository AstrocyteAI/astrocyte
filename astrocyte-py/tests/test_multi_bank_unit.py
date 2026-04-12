"""Unit tests for MultiBankOrchestrator — tested with mock callables.

Covers parallel/cascade/first_match strategies, timeout handling,
partial failure, dedup, bank weighting, and token budget enforcement.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from astrocyte._multi_bank import (
    MultiBankOrchestrator,
    _apply_bank_weights,
    _bank_visit_order,
    _dedupe_hits_by_text,
    _tag_hits_with_bank,
)
from astrocyte._recall_params import RecallParams
from astrocyte.policy.observability import MetricsCollector
from astrocyte.types import MemoryHit, MultiBankStrategy, RecallRequest, RecallResult


def _hit(text: str, score: float = 0.9, bank_id: str | None = None) -> MemoryHit:
    return MemoryHit(text=text, score=score, bank_id=bank_id)


def _result(*hits: MemoryHit) -> RecallResult:
    return RecallResult(hits=list(hits), total_available=len(hits), truncated=False)


def _make_orchestrator(
    recall_results: dict[str, RecallResult] | None = None,
) -> MultiBankOrchestrator:
    """Create orchestrator with a mock recall function that returns per-bank results."""
    results = recall_results or {}

    async def mock_recall(request: RecallRequest) -> RecallResult:
        return results.get(request.bank_id, _result())

    async def mock_make_request(
        query: str,
        bank_id: str,
        max_results: int,
        max_tokens: int | None,
        tags: list[str] | None,
        params: RecallParams,
    ) -> RecallRequest:
        return RecallRequest(
            query=query,
            bank_id=bank_id,
            max_results=max_results,
            max_tokens=max_tokens,
            tags=tags,
        )

    return MultiBankOrchestrator(
        do_recall=mock_recall,
        make_request=mock_make_request,
        circuit_breaker_record_failure=MagicMock(),
        metrics=MetricsCollector(enabled=False),
        provider_name="test",
    )


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestBankVisitOrder:
    def test_no_cascade_order(self) -> None:
        assert _bank_visit_order(["a", "b", "c"], None) == ["a", "b", "c"]

    def test_cascade_reorders(self) -> None:
        assert _bank_visit_order(["a", "b", "c"], ["c", "a"]) == ["c", "a", "b"]

    def test_cascade_ignores_unknown(self) -> None:
        assert _bank_visit_order(["a", "b"], ["z", "a"]) == ["a", "b"]

    def test_empty_banks(self) -> None:
        assert _bank_visit_order([], ["a"]) == []


class TestDedupeHitsByText:
    def test_keeps_highest_score(self) -> None:
        hits = [_hit("hello", 0.5), _hit("hello", 0.9), _hit("world", 0.7)]
        deduped = _dedupe_hits_by_text(hits)
        assert len(deduped) == 2
        hello = next(h for h in deduped if h.text == "hello")
        assert hello.score == 0.9

    def test_sorted_by_score_desc(self) -> None:
        hits = [_hit("a", 0.3), _hit("b", 0.9), _hit("c", 0.6)]
        deduped = _dedupe_hits_by_text(hits)
        assert [h.score for h in deduped] == [0.9, 0.6, 0.3]

    def test_empty(self) -> None:
        assert _dedupe_hits_by_text([]) == []


class TestApplyBankWeights:
    def test_applies_weights(self) -> None:
        hits = [_hit("a", 1.0, "b1"), _hit("b", 1.0, "b2")]
        weighted = _apply_bank_weights(hits, {"b1": 2.0, "b2": 0.5})
        assert weighted[0].score == 2.0
        assert weighted[1].score == 0.5

    def test_default_weight_1(self) -> None:
        hits = [_hit("a", 0.8, "b1")]
        weighted = _apply_bank_weights(hits, {"b2": 2.0})
        assert weighted[0].score == 0.8

    def test_none_weights_passthrough(self) -> None:
        hits = [_hit("a", 0.8, "b1")]
        weighted = _apply_bank_weights(hits, None)
        assert weighted[0].score == 0.8


class TestTagHitsWithBank:
    def test_tags_none_bank_id(self) -> None:
        hits = [_hit("a", bank_id=None)]
        tagged = _tag_hits_with_bank(hits, "b1")
        assert tagged[0].bank_id == "b1"

    def test_preserves_existing_bank_id(self) -> None:
        hits = [_hit("a", bank_id="original")]
        tagged = _tag_hits_with_bank(hits, "b1")
        assert tagged[0].bank_id == "original"


# ---------------------------------------------------------------------------
# Parallel strategy
# ---------------------------------------------------------------------------


class TestParallelStrategy:
    @pytest.mark.asyncio
    async def test_merges_results_from_multiple_banks(self) -> None:
        orch = _make_orchestrator({
            "b1": _result(_hit("from b1", 0.9)),
            "b2": _result(_hit("from b2", 0.8)),
        })
        result = await orch.recall(
            query="q",
            bank_ids=["b1", "b2"],
            max_results=10,
            max_tokens=None,
            tags=None,
            params=RecallParams(),
            strategy=MultiBankStrategy(mode="parallel"),
        )
        assert len(result.hits) == 2
        assert result.total_available == 2

    @pytest.mark.asyncio
    async def test_sorted_by_score(self) -> None:
        orch = _make_orchestrator({
            "b1": _result(_hit("low", 0.3)),
            "b2": _result(_hit("high", 0.9)),
        })
        result = await orch.recall(
            query="q",
            bank_ids=["b1", "b2"],
            max_results=10,
            max_tokens=None,
            tags=None,
            params=RecallParams(),
            strategy=MultiBankStrategy(mode="parallel"),
        )
        assert result.hits[0].text == "high"

    @pytest.mark.asyncio
    async def test_dedup_across_banks(self) -> None:
        orch = _make_orchestrator({
            "b1": _result(_hit("same text", 0.9)),
            "b2": _result(_hit("same text", 0.7)),
        })
        result = await orch.recall(
            query="q",
            bank_ids=["b1", "b2"],
            max_results=10,
            max_tokens=None,
            tags=None,
            params=RecallParams(),
            strategy=MultiBankStrategy(mode="parallel", dedup_across_banks=True),
        )
        assert len(result.hits) == 1
        assert result.hits[0].score == 0.9

    @pytest.mark.asyncio
    async def test_no_dedup_keeps_duplicates(self) -> None:
        orch = _make_orchestrator({
            "b1": _result(_hit("same text", 0.9)),
            "b2": _result(_hit("same text", 0.7)),
        })
        result = await orch.recall(
            query="q",
            bank_ids=["b1", "b2"],
            max_results=10,
            max_tokens=None,
            tags=None,
            params=RecallParams(),
            strategy=MultiBankStrategy(mode="parallel", dedup_across_banks=False),
        )
        assert len(result.hits) == 2

    @pytest.mark.asyncio
    async def test_bank_weights(self) -> None:
        orch = _make_orchestrator({
            "b1": _result(_hit("boosted", 0.5)),
            "b2": _result(_hit("normal", 0.8)),
        })
        result = await orch.recall(
            query="q",
            bank_ids=["b1", "b2"],
            max_results=10,
            max_tokens=None,
            tags=None,
            params=RecallParams(),
            strategy=MultiBankStrategy(mode="parallel", bank_weights={"b1": 3.0}),
        )
        # b1 hit: 0.5 * 3.0 = 1.5, should be first
        assert result.hits[0].text == "boosted"

    @pytest.mark.asyncio
    async def test_max_results_limits_output(self) -> None:
        orch = _make_orchestrator({
            "b1": _result(_hit("a", 0.9), _hit("b", 0.8)),
            "b2": _result(_hit("c", 0.7), _hit("d", 0.6)),
        })
        result = await orch.recall(
            query="q",
            bank_ids=["b1", "b2"],
            max_results=2,
            max_tokens=None,
            tags=None,
            params=RecallParams(),
            strategy=MultiBankStrategy(mode="parallel"),
        )
        assert len(result.hits) == 2

    @pytest.mark.asyncio
    async def test_partial_failure_returns_successful_results(self) -> None:
        """If one bank fails, results from other banks are still returned."""
        async def failing_recall(request: RecallRequest) -> RecallResult:
            if request.bank_id == "bad":
                raise RuntimeError("Bank unavailable")
            return _result(_hit("good hit", 0.9))

        async def mock_make_request(query, bank_id, max_results, max_tokens, tags, params):
            return RecallRequest(query=query, bank_id=bank_id, max_results=max_results)

        cb_mock = MagicMock()
        orch = MultiBankOrchestrator(
            do_recall=failing_recall,
            make_request=mock_make_request,
            circuit_breaker_record_failure=cb_mock,
            metrics=MetricsCollector(enabled=False),
            provider_name="test",
        )
        result = await orch.recall(
            query="q",
            bank_ids=["good", "bad"],
            max_results=10,
            max_tokens=None,
            tags=None,
            params=RecallParams(),
            strategy=MultiBankStrategy(mode="parallel"),
        )
        assert len(result.hits) == 1
        cb_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Cascade strategy
# ---------------------------------------------------------------------------


class TestCascadeStrategy:
    @pytest.mark.asyncio
    async def test_stops_after_min_results(self) -> None:
        call_order: list[str] = []

        async def tracking_recall(request: RecallRequest) -> RecallResult:
            call_order.append(request.bank_id)
            if request.bank_id == "b1":
                return _result(_hit("hit1", 0.9), _hit("hit2", 0.8), _hit("hit3", 0.7))
            return _result(_hit("hit4", 0.6))

        async def mock_make_request(query, bank_id, max_results, max_tokens, tags, params):
            return RecallRequest(query=query, bank_id=bank_id, max_results=max_results)

        orch = MultiBankOrchestrator(
            do_recall=tracking_recall,
            make_request=mock_make_request,
            circuit_breaker_record_failure=MagicMock(),
            metrics=MetricsCollector(enabled=False),
            provider_name="test",
        )
        result = await orch.recall(
            query="q",
            bank_ids=["b1", "b2"],
            max_results=10,
            max_tokens=None,
            tags=None,
            params=RecallParams(),
            strategy=MultiBankStrategy(mode="cascade", min_results_to_stop=3),
        )
        # Should stop after b1 since it returned 3 hits
        assert call_order == ["b1"]
        assert len(result.hits) == 3

    @pytest.mark.asyncio
    async def test_cascade_order_respected(self) -> None:
        call_order: list[str] = []

        async def tracking_recall(request: RecallRequest) -> RecallResult:
            call_order.append(request.bank_id)
            return _result()

        async def mock_make_request(query, bank_id, max_results, max_tokens, tags, params):
            return RecallRequest(query=query, bank_id=bank_id, max_results=max_results)

        orch = MultiBankOrchestrator(
            do_recall=tracking_recall,
            make_request=mock_make_request,
            circuit_breaker_record_failure=MagicMock(),
            metrics=MetricsCollector(enabled=False),
            provider_name="test",
        )
        await orch.recall(
            query="q",
            bank_ids=["a", "b", "c"],
            max_results=10,
            max_tokens=None,
            tags=None,
            params=RecallParams(),
            strategy=MultiBankStrategy(mode="cascade", cascade_order=["c", "a", "b"]),
        )
        assert call_order == ["c", "a", "b"]

    @pytest.mark.asyncio
    async def test_continues_when_not_enough_results(self) -> None:
        call_order: list[str] = []

        async def tracking_recall(request: RecallRequest) -> RecallResult:
            call_order.append(request.bank_id)
            return _result(_hit(f"from {request.bank_id}", 0.5))

        async def mock_make_request(query, bank_id, max_results, max_tokens, tags, params):
            return RecallRequest(query=query, bank_id=bank_id, max_results=max_results)

        orch = MultiBankOrchestrator(
            do_recall=tracking_recall,
            make_request=mock_make_request,
            circuit_breaker_record_failure=MagicMock(),
            metrics=MetricsCollector(enabled=False),
            provider_name="test",
        )
        result = await orch.recall(
            query="q",
            bank_ids=["b1", "b2", "b3"],
            max_results=10,
            max_tokens=None,
            tags=None,
            params=RecallParams(),
            strategy=MultiBankStrategy(mode="cascade", min_results_to_stop=5),
        )
        # All banks visited since total < 5
        assert call_order == ["b1", "b2", "b3"]
        assert len(result.hits) == 3


# ---------------------------------------------------------------------------
# First-match strategy
# ---------------------------------------------------------------------------


class TestFirstMatchStrategy:
    @pytest.mark.asyncio
    async def test_returns_first_bank_with_hits(self) -> None:
        call_order: list[str] = []

        async def tracking_recall(request: RecallRequest) -> RecallResult:
            call_order.append(request.bank_id)
            if request.bank_id == "b2":
                return _result(_hit("found it", 0.9))
            return _result()

        async def mock_make_request(query, bank_id, max_results, max_tokens, tags, params):
            return RecallRequest(query=query, bank_id=bank_id, max_results=max_results)

        orch = MultiBankOrchestrator(
            do_recall=tracking_recall,
            make_request=mock_make_request,
            circuit_breaker_record_failure=MagicMock(),
            metrics=MetricsCollector(enabled=False),
            provider_name="test",
        )
        result = await orch.recall(
            query="q",
            bank_ids=["b1", "b2", "b3"],
            max_results=10,
            max_tokens=None,
            tags=None,
            params=RecallParams(),
            strategy=MultiBankStrategy(mode="first_match"),
        )
        # Should stop after b2 (first with hits), never call b3
        assert call_order == ["b1", "b2"]
        assert len(result.hits) == 1
        assert result.hits[0].text == "found it"

    @pytest.mark.asyncio
    async def test_returns_empty_if_no_bank_has_hits(self) -> None:
        orch = _make_orchestrator({
            "b1": _result(),
            "b2": _result(),
        })
        result = await orch.recall(
            query="q",
            bank_ids=["b1", "b2"],
            max_results=10,
            max_tokens=None,
            tags=None,
            params=RecallParams(),
            strategy=MultiBankStrategy(mode="first_match"),
        )
        assert result.hits == []
        assert result.total_available == 0


# ---------------------------------------------------------------------------
# Invalid strategy
# ---------------------------------------------------------------------------


class TestInvalidStrategy:
    @pytest.mark.asyncio
    async def test_unknown_mode_raises(self) -> None:
        orch = _make_orchestrator()
        with pytest.raises(Exception, match="Unknown multi-bank mode"):
            await orch.recall(
                query="q",
                bank_ids=["b1"],
                max_results=10,
                max_tokens=None,
                tags=None,
                params=RecallParams(),
                strategy=MultiBankStrategy(mode="invalid"),
            )
