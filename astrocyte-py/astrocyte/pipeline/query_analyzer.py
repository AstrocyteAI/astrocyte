"""Query analyzer — structured extraction of temporal constraints.

Many recall queries embed temporal scoping ("what happened last spring?",
"who did Alice meet in March 2024?", "events before the launch"). The
default semantic-similarity recall treats time-words as just more text;
without parsing them out, the system can't filter or boost evidence by
date range, and temporal-category questions on benchmarks like LoCoMo
suffer.

This module exposes :func:`analyze_query`, which returns a
:class:`QueryAnalysis` describing the structured constraints embedded
in a query. Currently the only constraint type is
:class:`TemporalConstraint`; the API is shaped to accept additional
constraint types (location, entity, fact_type) without breaking
callers.

Two-tier extraction:

1. **Regex pre-pass** (no LLM cost): catches the high-volume common
   patterns — explicit ISO dates, year-only mentions, ``last <unit>``,
   ``in <month> [<year>]``, ``yesterday`` / ``today``. Bounded to ~15
   patterns to keep the path predictable.
2. **LLM fallback**: when the regex pass finds no match AND the query
   contains a temporal-marker token (configurable list), defer to an
   LLM call with a structured-JSON prompt. Skipped entirely when no
   temporal-marker is present — most queries don't mention time.

The fallback is opt-in via ``allow_llm_fallback=True``. Keeping the
default off makes ``analyze_query`` a fast, deterministic path that
recall can call on every request without budget concerns.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from astrocyte.types import Message

_logger = logging.getLogger("astrocyte.query_analyzer")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class TemporalConstraint:
    """A time range extracted from a query.

    Both endpoints are inclusive. Either may be ``None`` to express
    unbounded ("before March 2024" → ``end_date`` set, ``start_date``
    None; "since Q1 2024" → ``start_date`` set, ``end_date`` None).
    """

    start_date: datetime | None = None
    end_date: datetime | None = None

    def __str__(self) -> str:
        s = self.start_date.strftime("%Y-%m-%d") if self.start_date else "any"
        e = self.end_date.strftime("%Y-%m-%d") if self.end_date else "any"
        return f"{s} to {e}"

    def is_bounded(self) -> bool:
        return self.start_date is not None or self.end_date is not None


@dataclass
class QueryAnalysis:
    """Result of structured query analysis."""

    temporal_constraint: TemporalConstraint | None = None
    #: Why the analyzer flagged this constraint — short string for
    #: debugging / observability. Populated for both regex and LLM hits.
    rationale: str = ""

    def has_constraints(self) -> bool:
        return self.temporal_constraint is not None and self.temporal_constraint.is_bounded()


# ---------------------------------------------------------------------------
# Regex pre-pass
# ---------------------------------------------------------------------------


# Patterns that are clear enough to extract without an LLM call.
# Each entry returns ``(start, end, rationale)`` when matched. ``None``
# for either endpoint means open-ended.

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


def _utc(year: int, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _month_end(year: int, month: int) -> datetime:
    if month == 12:
        return _utc(year + 1, 1, 1) - timedelta(seconds=1)
    return _utc(year, month + 1, 1) - timedelta(seconds=1)


def _try_iso_date(query: str) -> tuple[datetime | None, datetime | None, str] | None:
    """Match explicit ISO dates: ``2024-03-15``, ``2024-03``, ``2024``."""
    # YYYY-MM-DD
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", query)
    if m:
        try:
            d = _utc(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, d + timedelta(days=1) - timedelta(seconds=1), f"explicit date {m.group(0)}"
        except ValueError:
            pass
    # YYYY-MM
    m = re.search(r"\b(\d{4})-(\d{2})\b", query)
    if m:
        try:
            year, month = int(m.group(1)), int(m.group(2))
            return _utc(year, month), _month_end(year, month), f"explicit month {m.group(0)}"
        except ValueError:
            pass
    return None


def _try_year(query: str) -> tuple[datetime | None, datetime | None, str] | None:
    """Match standalone 4-digit year (``in 2024``, ``during 2023``)."""
    m = re.search(r"\b(?:in|during|from)\s+(\d{4})\b", query, re.IGNORECASE)
    if m:
        year = int(m.group(1))
        if 1900 <= year <= 2100:
            return _utc(year), _utc(year + 1) - timedelta(seconds=1), f"year-only {year}"
    # Bare year token at start/end of clause.
    m = re.search(r"\b(\d{4})\b", query)
    if m:
        year = int(m.group(1))
        if 1900 <= year <= 2100:
            return _utc(year), _utc(year + 1) - timedelta(seconds=1), f"bare year {year}"
    return None


def _try_month_year(query: str) -> tuple[datetime | None, datetime | None, str] | None:
    """Match ``in March 2024``, ``March 2024``, ``March`` (with reference year)."""
    pattern = r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|sept|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?:\s+(\d{4}))?\b"
    m = re.search(pattern, query, re.IGNORECASE)
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    if month is None:
        return None
    year_str = m.group(2)
    if year_str:
        year = int(year_str)
        return _utc(year, month), _month_end(year, month), f"{m.group(1)} {year}"
    # Without an explicit year, we can't resolve — skip the regex hit.
    # The LLM fallback can attempt this with a reference_date.
    return None


def _try_relative(
    query: str, *, reference: datetime,
) -> tuple[datetime | None, datetime | None, str] | None:
    """Resolve relative expressions (yesterday, last week, X days ago)."""
    q = query.lower()
    # Yesterday / today
    if re.search(r"\byesterday\b", q):
        d = reference - timedelta(days=1)
        d = d.replace(hour=0, minute=0, second=0, microsecond=0)
        return d, d + timedelta(days=1) - timedelta(seconds=1), "yesterday"
    if re.search(r"\btoday\b", q):
        d = reference.replace(hour=0, minute=0, second=0, microsecond=0)
        return d, d + timedelta(days=1) - timedelta(seconds=1), "today"
    # Last <unit>
    m = re.search(r"\blast\s+(week|month|year)\b", q)
    if m:
        unit = m.group(1)
        if unit == "week":
            end = reference - timedelta(days=reference.weekday())
            start = end - timedelta(days=7)
            return start, end - timedelta(seconds=1), "last week"
        if unit == "month":
            year, month = reference.year, reference.month
            month -= 1
            if month == 0:
                month, year = 12, year - 1
            return _utc(year, month), _month_end(year, month), "last month"
        if unit == "year":
            year = reference.year - 1
            return _utc(year), _utc(year + 1) - timedelta(seconds=1), "last year"
    # N units ago
    m = re.search(r"\b(\d+)\s+(day|week|month|year)s?\s+ago\b", q)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "day":
            d = reference - timedelta(days=n)
            return d.replace(hour=0, minute=0, second=0, microsecond=0), \
                   reference, f"{n} day(s) ago"
        if unit == "week":
            d = reference - timedelta(weeks=n)
            return d, reference, f"{n} week(s) ago"
        # month/year — approximate; LLM fallback handles precision.
        if unit == "month":
            d = reference - timedelta(days=30 * n)
            return d, reference, f"~{n} month(s) ago"
        if unit == "year":
            d = reference - timedelta(days=365 * n)
            return d, reference, f"~{n} year(s) ago"
    return None


def _regex_temporal_pass(
    query: str, *, reference: datetime,
) -> TemporalConstraint | None:
    """Try each regex pattern in order; return the first match."""
    for fn in (_try_iso_date, _try_relative, _try_month_year, _try_year):
        if fn is _try_relative:
            hit = fn(query, reference=reference)  # type: ignore[arg-type]
        else:
            hit = fn(query)  # type: ignore[arg-type]
        if hit is not None:
            start, end, _rationale = hit
            return TemporalConstraint(start_date=start, end_date=end)
    return None


# ---------------------------------------------------------------------------
# Temporal-marker detection (cheap gate before LLM fallback)
# ---------------------------------------------------------------------------


_TEMPORAL_MARKERS = {
    "yesterday", "today", "tomorrow", "last", "this", "next",
    "ago", "before", "after", "since", "until", "during",
    "when", "while", "then", "now", "recently", "earlier",
    "later", "previous", "previously", "ever", "never", "always",
    "year", "month", "week", "day", "morning", "evening",
    "spring", "summer", "fall", "autumn", "winter",
}


def _has_temporal_marker(query: str) -> bool:
    """Cheap word-level test for temporal markers in the query."""
    tokens = re.findall(r"[a-z]+", query.lower())
    if not tokens:
        return False
    if any(t in _TEMPORAL_MARKERS for t in tokens):
        return True
    # Year mentions (4-digit) count as temporal too.
    return bool(re.search(r"\b\d{4}\b", query))


# ---------------------------------------------------------------------------
# LLM fallback
# ---------------------------------------------------------------------------


_LLM_SYSTEM_PROMPT = """\
You extract a TIME RANGE from a query when one is implied.

Output a JSON object: {"start_date": "<ISO date or null>", "end_date": \
"<ISO date or null>", "rationale": "<1 sentence>"}.

Rules:
1. If the query has no temporal scope, return {"start_date": null, \
"end_date": null, "rationale": "no temporal scope"}.
2. Both dates are inclusive. Use null for open-ended ranges \
("before March 2024" → end_date set, start_date null).
3. ISO 8601 with timezone: "2024-03-01T00:00:00Z".
4. Use the supplied reference date to resolve relative expressions \
("last spring" relative to a 2025-01-15 reference is "2024-03-01" to \
"2024-05-31").
5. Output JSON only. No prose.
"""


def _build_llm_user_prompt(query: str, reference: datetime) -> str:
    return (
        f"Reference date: {reference.isoformat()}\n"
        f"Query: {query.strip()}\n\n"
        f"Time range (JSON):"
    )


def _parse_llm_response(raw: str) -> TemporalConstraint | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    def _parse_iso(value) -> datetime | None:
        if not value or not isinstance(value, str):
            return None
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    start = _parse_iso(parsed.get("start_date"))
    end = _parse_iso(parsed.get("end_date"))
    if start is None and end is None:
        return None
    return TemporalConstraint(start_date=start, end_date=end)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def analyze_query(
    query: str,
    *,
    reference_date: datetime | None = None,
    llm_provider=None,
    allow_llm_fallback: bool = False,
) -> QueryAnalysis:
    """Extract structured constraints from a query.

    Args:
        query: User question / recall query.
        reference_date: Used to resolve relative expressions ("last
            week", "yesterday"). Defaults to ``datetime.now(UTC)``.
        llm_provider: Required when ``allow_llm_fallback=True``.
            Passed to the LLM call when the regex pre-pass misses.
        allow_llm_fallback: When True, falls back to an LLM call after
            the regex pass when (a) no regex matched AND (b) the query
            contains a temporal-marker token. When False, only the
            regex path runs (deterministic, no LLM cost). Default
            False so callers explicitly opt in.

    Returns:
        :class:`QueryAnalysis` whose ``temporal_constraint`` is set
        when extraction succeeded. ``has_constraints()`` is the
        idiomatic check.
    """
    if not query or not query.strip():
        return QueryAnalysis()

    ref = reference_date or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)

    # Regex pre-pass.
    regex_hit = _regex_temporal_pass(query, reference=ref)
    if regex_hit is not None:
        return QueryAnalysis(
            temporal_constraint=regex_hit,
            rationale="regex match",
        )

    # LLM fallback (gated).
    if not allow_llm_fallback or llm_provider is None:
        return QueryAnalysis()
    if not _has_temporal_marker(query):
        # No temporal-marker token — the LLM is unlikely to find a
        # constraint that the regex missed. Save the call.
        return QueryAnalysis()

    try:
        completion = await llm_provider.complete(
            [
                Message(role="system", content=_LLM_SYSTEM_PROMPT),
                Message(role="user", content=_build_llm_user_prompt(query, ref)),
            ],
            max_tokens=256,
            temperature=0.0,
        )
    except Exception as exc:
        _logger.warning("query_analyzer LLM fallback failed (%s)", exc)
        return QueryAnalysis()

    constraint = _parse_llm_response(completion.text)
    if constraint is None or not constraint.is_bounded():
        return QueryAnalysis()
    return QueryAnalysis(
        temporal_constraint=constraint,
        rationale="llm fallback",
    )
