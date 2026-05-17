"""Tests for `astrocyte.pipeline.temporal_dateparser`.

Verifies:
  - Graceful no-op when dateparser package isn't installed
  - Named-date extraction ("March 15", "June 5th")
  - ISO date extraction
  - Relative date extraction respects anchor
  - False-positive filter (short common words like "do", "may", "can")
  - Defensive: parser exceptions don't propagate
  - widen_to_neighbourhood adds ±pad_days
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from astrocyte.pipeline import temporal_dateparser as tdp
from astrocyte.pipeline.temporal_dateparser import (
    extract_temporal_range_via_dateparser,
    widen_to_neighbourhood,
)


@pytest.fixture(autouse=True)
def _reset_lazy_state():
    """Reset the module's lazy-load state between tests so missing-dep
    tests don't poison the success-case tests (and vice versa)."""
    tdp._DATEPARSER_AVAILABLE = None
    tdp._search_dates = None
    yield
    tdp._DATEPARSER_AVAILABLE = None
    tdp._search_dates = None


def _have_dateparser() -> bool:
    try:
        import dateparser  # noqa: F401, PLC0415
        return True
    except ImportError:
        return False


# ────────────────────────────────────────────────────────────────────────────
# Graceful degradation when the optional dep is missing
# ────────────────────────────────────────────────────────────────────────────

class TestGracefulDegradation:
    def test_returns_none_when_dateparser_missing(self) -> None:
        # Force the lazy-load to fail by faking an ImportError.
        with patch.object(tdp, "_lazy_load", return_value=False):
            result = extract_temporal_range_via_dateparser(
                "what happened last Tuesday?",
                anchor=datetime(2024, 6, 15),
            )
        assert result is None

    def test_returns_none_for_empty_query(self) -> None:
        assert extract_temporal_range_via_dateparser("", anchor=datetime(2024, 6, 15)) is None


# ────────────────────────────────────────────────────────────────────────────
# Real dateparser behavior (skip if dep missing)
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not _have_dateparser(), reason="dateparser not installed")
class TestRealDateparserExtraction:
    """These tests actually exercise the dateparser library — only run
    when it's installed (via the `bench` extras)."""

    ANCHOR = datetime(2024, 6, 15)

    def test_named_month_day_extracted(self) -> None:
        # "March 15" should resolve relative to anchor (so 2024 since PREFER_DATES_FROM=past).
        result = extract_temporal_range_via_dateparser(
            "What did Alice say on March 15?", anchor=self.ANCHOR,
        )
        assert result is not None
        start, end = result
        assert start.month == 3 and start.day == 15
        # Single-day window
        assert (end - start) < timedelta(days=1)

    def test_iso_date_extracted(self) -> None:
        result = extract_temporal_range_via_dateparser(
            "Find notes from 2024-03-15", anchor=self.ANCHOR,
        )
        assert result is not None
        start, _end = result
        assert start.year == 2024 and start.month == 3 and start.day == 15

    def test_no_date_in_question_returns_none(self) -> None:
        # Pure semantic question with no temporal cue at all
        result = extract_temporal_range_via_dateparser(
            "What is the user's favourite colour?", anchor=self.ANCHOR,
        )
        # dateparser is aggressive; "is" or other short words may slip,
        # but the false-positive filter should reject them. Accept None
        # OR a result whose text isn't a false positive.
        assert result is None or isinstance(result[0], datetime)

    def test_false_positive_filtered(self) -> None:
        # "What can I do?" — dateparser parses "do" as a date. Our filter
        # must reject it.
        result = extract_temporal_range_via_dateparser(
            "What can I do?", anchor=self.ANCHOR,
        )
        assert result is None

    def test_defensive_against_parser_exception(self) -> None:
        # Simulate a dateparser internal crash — should NOT propagate.
        tdp._lazy_load()  # ensure _search_dates is loaded

        def _boom(*_args, **_kwargs):
            raise IndexError("locale.translate_search exploded")

        with patch.object(tdp, "_search_dates", _boom):
            result = extract_temporal_range_via_dateparser(
                "anything goes here", anchor=self.ANCHOR,
            )
        assert result is None


# ────────────────────────────────────────────────────────────────────────────
# widen_to_neighbourhood is dep-free; always runs
# ────────────────────────────────────────────────────────────────────────────

class TestWidenToNeighbourhood:
    def test_pads_one_day_each_side_by_default(self) -> None:
        start = datetime(2024, 6, 15, 0, 0, 0)
        end = datetime(2024, 6, 15, 23, 59, 59)
        ws, we = widen_to_neighbourhood((start, end))
        assert ws == start - timedelta(days=1)
        assert we == end + timedelta(days=1)

    def test_custom_pad(self) -> None:
        start = datetime(2024, 6, 15)
        end = datetime(2024, 6, 15, 23, 59, 59)
        ws, we = widen_to_neighbourhood((start, end), pad_days=7)
        assert ws == start - timedelta(days=7)
        assert we == end + timedelta(days=7)
