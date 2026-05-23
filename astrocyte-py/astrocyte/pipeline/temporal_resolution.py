"""M31 Fix 4 — Temporal resolution at retain time.

Resolves relative date phrases in a fact's text ("last Tuesday",
"3 days ago", "two weeks back") to an absolute :class:`datetime`
**at retain time**, using the fact's anchoring section's
``session_date`` as the reference base.

The resolved value is stored as :attr:`MemoryFact.event_date`. The
answerer renders it instead of forcing the gpt-4o-mini LLM to do
date arithmetic at query time — gpt-4o-mini is unreliable at date
math (the 60–70% temporal-reasoning ceiling we keep hitting in
LME). Moving the deterministic part of the work (regex + library
parsing) out of the LLM call is an architectural divergence from
Hindsight, which keeps date math in the LLM-side prompt.

Design notes
------------

- **Single-call retain-time use only.** This module does NOT run at
  query time — the existing :mod:`astrocyte.pipeline.temporal_dateparser`
  handles that path with a different contract (date-range extraction
  from the question).
- **No LLM cost.** Pure regex + library work. Per-fact cost is
  measured in microseconds; safe to run on every extracted fact.
- **Best-effort.** Returns ``None`` when:
  - The text has no recognisable date phrase
  - dateparser raises (it has known bugs on certain locales / inputs)
  - The first valid match is a false-positive short token
    (mirrors the filter in ``temporal_dateparser``)
- **Single datetime output.** Unlike the query-time extractor
  which returns ``(start, end)``, retain-time resolution snaps to
  one datetime (00:00:00 on the resolved date). The fact's
  ``occurred_start`` / ``occurred_end`` are LLM-emitted ranges
  for events that span multiple days; ``event_date`` is the
  single most-prominent absolute date for this fact.
- **Anchor semantics.** When ``anchor`` is None (top-level facts
  without a section context), we cannot resolve relative phrases
  and return ``None``. Absolute phrases like "March 15, 2024"
  would parse without an anchor, but the resulting datetime would
  not be timezone-consistent with the rest of the system, so we
  conservatively skip these too.
"""

from __future__ import annotations

import logging
from datetime import datetime

_logger = logging.getLogger("astrocyte.pipeline.temporal_resolution")

# Lazy-load dateparser (same pattern as temporal_dateparser).
_DATEPARSER_AVAILABLE: bool | None = None
_search_dates = None  # type: ignore[var-annotated]

# False-positive filter — short tokens that dateparser misparses as
# dates ("on", "or", "in", "may", "march", etc). Mirrors the set in
# ``temporal_dateparser._FALSE_POSITIVES`` so the two extractors
# share filter discipline.
_FALSE_POSITIVES: set[str] = {
    "on", "in", "at", "to", "is", "or", "by", "as", "an", "a",
    "may", "march", "the", "and", "for",
}


def _lazy_load() -> bool:
    """Import dateparser on first use, cache the result."""
    global _DATEPARSER_AVAILABLE, _search_dates
    if _DATEPARSER_AVAILABLE is not None:
        return _DATEPARSER_AVAILABLE
    try:
        from dateparser.search import search_dates  # noqa: PLC0415

        _search_dates = search_dates
        _DATEPARSER_AVAILABLE = True
    except ImportError:
        _DATEPARSER_AVAILABLE = False
        _logger.info(
            "temporal_resolution: dateparser not installed; "
            "event_date resolution disabled (facts retain occurred_start only)."
        )
    return _DATEPARSER_AVAILABLE


def resolve_event_date(
    text: str,
    anchor: datetime | None,
) -> datetime | None:
    """Resolve the first relative date phrase in ``text`` to an absolute
    datetime, using ``anchor`` as the reference base for relative phrases.

    Args:
        text: The fact's text (or any natural-language fragment).
        anchor: Reference "now" for relative phrases. Typically the
            section's ``session_date`` (when the fact was mentioned).
            ``None`` disables resolution — we don't want absolute-only
            parses without a known timezone context.

    Returns:
        A datetime snapped to 00:00:00 on the resolved date, or
        ``None`` when no valid relative date is found.

    Examples
    --------
        >>> from datetime import datetime
        >>> anchor = datetime(2024, 5, 8)
        >>> resolve_event_date("I went to the doctor last Tuesday", anchor)
        datetime.datetime(2024, 5, 7, 0, 0)
        >>> resolve_event_date("no date here", anchor) is None
        True
    """
    if not text or anchor is None:
        return None
    if not _lazy_load():
        return None

    settings = {
        "RELATIVE_BASE": anchor,
        "PREFER_DATES_FROM": "past",
        "RETURN_AS_TIMEZONE_AWARE": False,
    }

    try:
        results = _search_dates(text, settings=settings)  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001
        _logger.debug(
            "temporal_resolution: dateparser raised %s on text=%r",
            type(exc).__name__, text[:80],
        )
        return None

    if not results:
        return None

    for matched_text, parsed in results:
        t = matched_text.strip().lower()
        # Same false-positive filter as the query-time extractor.
        if t in _FALSE_POSITIVES and len(t) <= 4:
            continue
        if len(t) <= 2:
            continue
        # Snap to start-of-day for a stable comparable timestamp.
        return parsed.replace(hour=0, minute=0, second=0, microsecond=0)

    return None


__all__ = ["resolve_event_date"]
