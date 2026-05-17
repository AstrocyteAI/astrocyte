"""Dateparser-based temporal extraction (Hindsight-parity Pass B).

The hand-rolled regex passes in ``temporal_expressions.py`` (Pass A)
cover fuzzy/range expressions like "a few weeks ago" and "couple of
months ago" that a general date parser would mishandle as single
points. But they miss the **vast majority** of temporal expressions
that real benchmark questions contain:

  - Named dates: "March 15", "the 3rd of June"
  - ISO dates: "2024-06-01"
  - Weekdays: "Tuesday", "last Friday"
  - Ordinals: "the 5th"
  - Implicit relative: "2 weeks ago" (covered by Pass A regex too)
  - Multi-language: "ayer" (Spanish), "letztes Jahr" (German)

The ``dateparser`` library covers all of the above with a single API
(``dateparser.search.search_dates``). Hindsight uses this as their
default temporal analyzer; their codebase shows two production lessons
we copy verbatim:

  1. **Defensive try/except** — dateparser has been observed to crash
     with internal errors (IndexError from locale.translate_search and
     similar) on certain inputs. A parser bug must NOT bring down the
     retrieval pipeline. Treat any failure as "no constraint found"
     and fall back to non-temporal recall.

  2. **False-positive filter** for short common words that
     dateparser misparses as dates: ``{"do", "may", "march", "will",
     "can", "sat", "sun", "mon", ...}``. Without this filter, the
     question "What can I do?" extracts "do" as a date.

This is Pass B in the chain: it runs AFTER the precise regex passes
(``_try_iso_date``, ``_try_relative``, ``_try_temporal_expansion``,
``_try_month_year``, ``_try_year``) so narrower exact matches still
win. Pass B is the **wide-net catchall** for everything else.

Public API:
    extract_temporal_range_via_dateparser(query, anchor)
        -> tuple[datetime, datetime] | None
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

_logger = logging.getLogger("astrocyte.pipeline.temporal_dateparser")

# Set once on first failed import so we don't spam logs every recall.
_DATEPARSER_AVAILABLE: bool | None = None
_search_dates = None  # type: ignore[var-annotated]

# Short tokens that dateparser frequently misparses as dates. Anything
# of length ≤ 3 in this set is filtered out (longer hits like
# "march" inside "Marching band" can still be a real cue when surrounded
# by clear date context; dateparser's own context handling decides).
# Mirrors Hindsight's set; kept English-only because our benches are
# English-only (LME, LoCoMo).
_FALSE_POSITIVES: frozenset[str] = frozenset(
    {
        "do", "may", "march", "will", "can",
        "sat", "sun", "mon", "tue", "wed", "thu", "fri",
        "i", "a", "an", "the", "is", "it",
    }
)


def _lazy_load() -> bool:
    """Lazy-import dateparser. Returns True on success, False if the
    package isn't installed. Logs once on missing-dep so the recall
    path stays quiet thereafter."""
    global _DATEPARSER_AVAILABLE, _search_dates
    if _DATEPARSER_AVAILABLE is not None:
        return _DATEPARSER_AVAILABLE
    try:
        from dateparser.search import search_dates  # noqa: PLC0415

        _search_dates = search_dates
        # Warm-up call — triggers lazy-loaded regex tables / locale
        # data so the first real recall doesn't pay the cold-start.
        try:
            _search_dates("today")
        except Exception:  # noqa: BLE001
            pass
        _DATEPARSER_AVAILABLE = True
    except ImportError:
        _logger.info(
            "temporal_dateparser: 'dateparser' not installed; "
            "Pass B disabled. Install with `pip install dateparser` "
            "or via the `bench` extra to enable.",
        )
        _DATEPARSER_AVAILABLE = False
    return _DATEPARSER_AVAILABLE


def extract_temporal_range_via_dateparser(
    query: str,
    anchor: datetime,
) -> tuple[datetime, datetime] | None:
    """Extract a date range from ``query`` using the dateparser library.

    Returns ``(start, end)`` for a single-day window centered on the
    first valid date found, or ``None`` when no date is found, the
    dependency is missing, or the only matches are filtered false
    positives.

    The returned range is a single day [00:00:00, 23:59:59.999999] —
    callers that want a wider window should widen it themselves. This
    mirrors Hindsight's contract.

    Args:
      query: The user's question.
      anchor: Reference "now" for relative expressions (e.g., the
        document's latest session timestamp).
    """
    if not query:
        return None
    if not _lazy_load():
        return None

    settings = {
        "RELATIVE_BASE": anchor,
        "PREFER_DATES_FROM": "past",
        "RETURN_AS_TIMEZONE_AWARE": False,
    }

    # Wrap the parser call in try/except — dateparser has known bugs
    # (IndexError from locale.translate_search, KeyError from broken
    # internal tables) that must not crash the recall pipeline.
    try:
        results = _search_dates(query, settings=settings)  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "temporal_dateparser: dateparser raised %s; "
            "treating as no temporal constraint. query=%r",
            type(exc).__name__, query[:80],
        )
        return None

    if not results:
        return None

    # Filter false positives. Hindsight's rule: a short token (≤3 chars
    # OR in the false-positive set with length≤4) gets dropped because
    # dateparser misparses common words as dates.
    valid: list[tuple[str, datetime]] = []
    for text, parsed in results:
        t = text.strip().lower()
        if t in _FALSE_POSITIVES and len(t) <= 4:
            continue
        if len(t) <= 2:
            # Two-letter tokens are almost never legitimate dates.
            continue
        valid.append((text, parsed))

    if not valid:
        return None

    # Use the first valid date. Hindsight does the same — multi-date
    # disambiguation is the LLM-fallback path's job, not the cheap
    # extractor's.
    _, parsed_date = valid[0]
    start = parsed_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end = parsed_date.replace(hour=23, minute=59, second=59, microsecond=999999)
    return (start, end)


def widen_to_neighbourhood(
    range_: tuple[datetime, datetime],
    *,
    pad_days: int = 1,
) -> tuple[datetime, datetime]:
    """Widen a single-day dateparser hit by ``pad_days`` on each side.

    The exact-day hit from dateparser is often too tight for fact-grain
    retrieval: a question about "what happened on June 5th" may be
    answered by a fact dated June 4th or June 6th. Callers should widen
    the dateparser range before handing it to ``search_facts_temporal``.

    Default pad of 1 day yields a 3-day window. Use larger pads for
    narrower-resolution fact corpora.
    """
    start, end = range_
    return (start - timedelta(days=pad_days), end + timedelta(days=pad_days))
