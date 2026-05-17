"""Query-time relative-temporal expansion.

Maps relative temporal expressions in a question (``"a few weeks ago"``,
``"last month"``, ``"3 days ago"``) to absolute ISO date ranges using a
reference anchor date. Recall code can then feed the range to the
fact-grain temporal-search SPI without depending on every ingested
section having a structured ``occurred_start`` populated.

Two prior attempts at INGEST-TIME structured-date extraction (M14.x,
M15.x) were reverted because per-fact temporal metadata is sparse —
many preference/opinion facts have no specific event time and stamping
one was net-negative across categories. Doing the expansion at QUERY
time avoids that problem: we widen recall when the question itself
asks about a time window, leaving ingest extraction untouched.

Anchor date selection (caller-supplied): use the latest session
timestamp known for the document. "A few weeks ago" relative to a
conversation that ran in May 2023 should map to mid-April 2023, not
relative to wall-clock today.

Conservative behaviour: if no temporal cue is found in the query,
return ``None``. Callers should fall back to non-temporal recall.

Supported expressions (case-insensitive, allowed anywhere in the
query string):

  - ``yesterday``                    → (anchor − 2d, anchor)
  - ``today``                        → (anchor − 1d, anchor + 1d)
  - ``last week`` / ``a week ago``   → (anchor − 14d, anchor − 5d)
  - ``this week``                    → (anchor − 8d, anchor + 1d)
  - ``last month`` / ``a month ago`` → (anchor − 60d, anchor − 20d)
  - ``last year`` / ``a year ago``   → (anchor − 540d, anchor − 270d)
  - ``a few <unit>s ago``            → (anchor − 5×unit, anchor − 2×unit)
  - ``couple <unit>s ago``           → (anchor − 4×unit, anchor − 2×unit)
  - ``<N> <unit>s ago``              → (anchor − (N+2)×unit, anchor − max(N−2,0)×unit)
  - ``earlier this <unit>``          → (anchor − 1×unit, anchor)
  - ``recently`` / ``just``          → (anchor − 14d, anchor + 1d)

Each "match" widens the window slightly so the recall SPI returns a
useful neighbourhood instead of an exact day-match (the user said "a
few weeks ago"; they mean roughly 2-5 weeks).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger("astrocyte.pipeline.temporal_expressions")


DateRange = tuple[datetime, datetime]


_UNIT_TO_DAYS: dict[str, int] = {
    "day": 1,
    "days": 1,
    "week": 7,
    "weeks": 7,
    "month": 30,
    "months": 30,
    "year": 365,
    "years": 365,
}


_NUMBER_WORD: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _coerce_n(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        try:
            return int(token)
        except ValueError:
            return None
    return _NUMBER_WORD.get(token)


def _range_centred_on(anchor: datetime, days_ago: float, half_width_days: float) -> DateRange:
    """Window: ``[anchor − (days_ago + half_width), anchor − (days_ago − half_width)]``.

    Clamps the lower bound to never go past 5 years before anchor (any
    document older than that is well outside the LME/LoCoMo bench scope
    and likely a parse error).
    """
    start = anchor - timedelta(days=days_ago + half_width_days)
    end = anchor - timedelta(days=max(days_ago - half_width_days, 0))
    floor = anchor - timedelta(days=365 * 5)
    if start < floor:
        start = floor
    return (start, end)


def expand_temporal_expression(query: str, anchor: datetime) -> DateRange | None:
    """Parse the first relative-time expression in ``query`` and return
    a date range. Returns ``None`` when no cue is found.

    ``anchor`` is the reference "now" for relative expressions —
    typically the latest session timestamp for the document.
    """
    if not query:
        return None
    q = query.lower()

    # "<N> <unit>s ago" — digit or word number, plural or singular unit
    m = re.search(
        r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
        r"(day|days|week|weeks|month|months|year|years)\s+ago\b",
        q,
    )
    if m:
        n = _coerce_n(m.group(1))
        unit_days = _UNIT_TO_DAYS.get(m.group(2))
        if n is not None and unit_days is not None:
            days_ago = n * unit_days
            half = max(unit_days, 1)
            return _range_centred_on(anchor, days_ago, half)

    # "a few <unit>s ago" — vague but pinned to roughly 2-5 units
    m = re.search(
        r"\b(a\s+few|few)\s+(day|days|week|weeks|month|months|year|years)\s+ago\b",
        q,
    )
    if m:
        unit_days = _UNIT_TO_DAYS.get(m.group(2))
        if unit_days is not None:
            return _range_centred_on(anchor, 3.5 * unit_days, 1.5 * unit_days)

    # "couple <unit>s ago" — ~2-3 units
    m = re.search(
        r"\b(a\s+couple\s+of|couple\s+of|a\s+couple|couple)\s+"
        r"(day|days|week|weeks|month|months|year|years)\s+ago\b",
        q,
    )
    if m:
        unit_days = _UNIT_TO_DAYS.get(m.group(2))
        if unit_days is not None:
            return _range_centred_on(anchor, 2.5 * unit_days, 1.0 * unit_days)

    # "last <unit>" or "<unit> ago" (no quantifier)
    m = re.search(
        r"\b(last|a)\s+(day|week|month|year)\b|"
        r"\bthe\s+other\s+(day|week|month)\b",
        q,
    )
    if m:
        unit = (m.group(2) or m.group(3) or "").lower()
        unit_days = _UNIT_TO_DAYS.get(unit)
        if unit_days is not None:
            # "last week" → 5-14 days ago; "last month" → 20-60d; "last year" → 270-540d
            return _range_centred_on(anchor, 1.4 * unit_days, 0.7 * unit_days)

    # "this <unit>" — current period
    m = re.search(r"\bthis\s+(week|month|year)\b", q)
    if m:
        unit_days = _UNIT_TO_DAYS.get(m.group(1))
        if unit_days is not None:
            return (anchor - timedelta(days=unit_days), anchor + timedelta(days=1))

    # "earlier this <unit>" — same window as "this <unit>"
    m = re.search(r"\bearlier\s+this\s+(week|month|year)\b", q)
    if m:
        unit_days = _UNIT_TO_DAYS.get(m.group(1))
        if unit_days is not None:
            return (anchor - timedelta(days=unit_days), anchor)

    # Single-word time anchors
    if re.search(r"\byesterday\b", q):
        return (anchor - timedelta(days=2), anchor)
    if re.search(r"\btoday\b", q):
        return (anchor - timedelta(days=1), anchor + timedelta(days=1))
    if re.search(r"\brecently\b|\bjust\s+now\b|\blately\b", q):
        return (anchor - timedelta(days=14), anchor + timedelta(days=1))

    return None
