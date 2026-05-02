"""Tests for query_analyzer.

Three tiers:

1. Regex pre-pass — catches explicit ISO dates, year-only mentions,
   "last <unit>", "in <Month> <Year>", "yesterday/today", "N units ago".
   Deterministic, no LLM cost.

2. Temporal-marker gate — when no regex matches, only proceed to LLM
   fallback when the query mentions a temporal-marker token.

3. LLM fallback — structured-JSON extraction for the ambiguous middle
   band (e.g. "What did Alice do last spring?" relative to a
   reference_date — needs the LLM to map seasons → months).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from astrocyte.pipeline.query_analyzer import (
    TemporalConstraint,
    _has_temporal_marker,
    analyze_query,
)
from astrocyte.testing.in_memory import MockLLMProvider
from astrocyte.types import Completion, Message, TokenUsage


class _ScriptedLLM(MockLLMProvider):
    def __init__(self, response: str) -> None:
        super().__init__(default_response=response)
        self.call_count = 0

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tools=None,
        tool_choice=None,
    ) -> Completion:
        self.call_count += 1
        return Completion(
            text=self._default_response, model="mock",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
        )


REF = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Regex pre-pass
# ---------------------------------------------------------------------------


class TestRegexPrepass:
    @pytest.mark.asyncio
    async def test_iso_date_full(self):
        result = await analyze_query(
            "What happened on 2024-03-15?", reference_date=REF,
        )
        c = result.temporal_constraint
        assert c is not None
        assert c.start_date == datetime(2024, 3, 15, tzinfo=UTC)
        assert c.end_date.day == 15

    @pytest.mark.asyncio
    async def test_iso_year_month(self):
        result = await analyze_query(
            "Summarize 2024-03 events", reference_date=REF,
        )
        c = result.temporal_constraint
        assert c is not None
        assert c.start_date == datetime(2024, 3, 1, tzinfo=UTC)
        assert c.end_date.month == 3
        assert c.end_date.day == 31

    @pytest.mark.asyncio
    async def test_year_in_phrase(self):
        result = await analyze_query("What was important in 2023?", reference_date=REF)
        c = result.temporal_constraint
        assert c is not None
        assert c.start_date == datetime(2023, 1, 1, tzinfo=UTC)
        assert c.end_date.year == 2023

    @pytest.mark.asyncio
    async def test_yesterday_uses_reference_date(self):
        result = await analyze_query("What did we discuss yesterday?", reference_date=REF)
        c = result.temporal_constraint
        assert c is not None
        # ref is 2025-06-15 → yesterday is 2025-06-14
        assert c.start_date.date().isoformat() == "2025-06-14"

    @pytest.mark.asyncio
    async def test_last_week(self):
        result = await analyze_query("What happened last week?", reference_date=REF)
        c = result.temporal_constraint
        assert c is not None
        # Last week ends at start-of-current-week (Monday); start is 7 days before.
        assert (c.end_date - c.start_date).days == 6 or (c.end_date - c.start_date).days == 7

    @pytest.mark.asyncio
    async def test_last_month(self):
        result = await analyze_query("any updates last month?", reference_date=REF)
        c = result.temporal_constraint
        assert c is not None
        assert c.start_date.month == 5  # May 2025
        assert c.end_date.month == 5

    @pytest.mark.asyncio
    async def test_n_days_ago(self):
        result = await analyze_query("what was 3 days ago", reference_date=REF)
        c = result.temporal_constraint
        assert c is not None
        assert c.start_date.date().isoformat() == "2025-06-12"

    @pytest.mark.asyncio
    async def test_month_with_year(self):
        result = await analyze_query("what happened in March 2024?", reference_date=REF)
        c = result.temporal_constraint
        assert c is not None
        assert c.start_date == datetime(2024, 3, 1, tzinfo=UTC)
        assert c.end_date.month == 3

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self):
        result = await analyze_query("Who is Alice?", reference_date=REF)
        assert result.temporal_constraint is None
        assert result.has_constraints() is False

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self):
        result = await analyze_query("", reference_date=REF)
        assert result.temporal_constraint is None


# ---------------------------------------------------------------------------
# Temporal-marker gate
# ---------------------------------------------------------------------------


class TestTemporalMarkerGate:
    def test_marker_words_detected(self):
        assert _has_temporal_marker("yesterday")
        assert _has_temporal_marker("what about last spring?")
        assert _has_temporal_marker("two weeks ago")
        assert _has_temporal_marker("during summer")

    def test_year_token_counts(self):
        assert _has_temporal_marker("anything from 2024?")

    def test_no_marker_returns_false(self):
        assert _has_temporal_marker("Who is Alice?") is False
        assert _has_temporal_marker("what does she like?") is False


# ---------------------------------------------------------------------------
# LLM fallback (gated)
# ---------------------------------------------------------------------------


class TestLLMFallback:
    @pytest.mark.asyncio
    async def test_off_by_default_no_llm_call(self):
        """``allow_llm_fallback=False`` (default) — no LLM call is made
        even when a temporal marker is present and regex misses."""
        llm = _ScriptedLLM('{"start_date": "2024-03-01T00:00:00Z", "end_date": "2024-05-31T00:00:00Z"}')

        result = await analyze_query(
            "What did Alice do last spring?",  # 'spring' = marker, no regex match
            reference_date=REF,
            llm_provider=llm,
            # allow_llm_fallback=False (default)
        )

        assert result.temporal_constraint is None
        assert llm.call_count == 0

    @pytest.mark.asyncio
    async def test_fallback_extracts_constraint(self):
        """Enabled fallback + temporal marker → LLM resolves the range."""
        llm = _ScriptedLLM(
            '{"start_date": "2024-03-01T00:00:00Z",'
            ' "end_date": "2024-05-31T00:00:00Z",'
            ' "rationale": "spring 2024"}'
        )

        result = await analyze_query(
            "What did Alice do last spring?",
            reference_date=REF,
            llm_provider=llm,
            allow_llm_fallback=True,
        )

        assert result.temporal_constraint is not None
        assert result.temporal_constraint.start_date == datetime(2024, 3, 1, tzinfo=UTC)
        assert result.rationale == "llm fallback"

    @pytest.mark.asyncio
    async def test_fallback_skipped_when_no_temporal_marker(self):
        """When the query has no temporal-marker token, even an enabled
        fallback short-circuits without an LLM call (cost guard)."""
        llm = _ScriptedLLM('{"start_date": null, "end_date": null}')

        result = await analyze_query(
            "Who is Alice?",
            reference_date=REF,
            llm_provider=llm,
            allow_llm_fallback=True,
        )

        assert result.temporal_constraint is None
        assert llm.call_count == 0

    @pytest.mark.asyncio
    async def test_fallback_handles_null_response(self):
        """LLM says no temporal scope → analyzer returns no constraint."""
        llm = _ScriptedLLM(
            '{"start_date": null, "end_date": null, "rationale": "no scope"}'
        )

        result = await analyze_query(
            "What did Alice do last spring?",
            reference_date=REF,
            llm_provider=llm,
            allow_llm_fallback=True,
        )

        assert result.temporal_constraint is None

    @pytest.mark.asyncio
    async def test_fallback_handles_malformed_response(self):
        """Garbage LLM response → analyzer returns no constraint."""
        llm = _ScriptedLLM("not JSON")

        result = await analyze_query(
            "What did Alice do last spring?",
            reference_date=REF,
            llm_provider=llm,
            allow_llm_fallback=True,
        )

        assert result.temporal_constraint is None

    @pytest.mark.asyncio
    async def test_regex_short_circuits_before_llm(self):
        """Regex hit prevents even an enabled LLM fallback from running."""
        llm = _ScriptedLLM("{}")

        result = await analyze_query(
            "What happened on 2024-03-15?",
            reference_date=REF,
            llm_provider=llm,
            allow_llm_fallback=True,
        )

        assert result.temporal_constraint is not None
        assert result.rationale == "regex match"
        assert llm.call_count == 0


class TestQueryAnalysisHelpers:
    def test_temporal_constraint_str(self):
        c = TemporalConstraint(
            start_date=datetime(2024, 3, 1, tzinfo=UTC),
            end_date=datetime(2024, 3, 31, tzinfo=UTC),
        )
        assert str(c) == "2024-03-01 to 2024-03-31"

    def test_temporal_constraint_unbounded_start(self):
        c = TemporalConstraint(end_date=datetime(2024, 3, 1, tzinfo=UTC))
        assert "any" in str(c)
        assert c.is_bounded()
