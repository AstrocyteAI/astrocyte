"""Lightweight temporal phrase detection for recall/reflect planning.

The helpers here do not try to become a full natural-language date parser.
They surface deterministic hints that the synthesis prompt can use to resolve
LoCoMo-style relative phrases against memory timestamps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class TemporalHint:
    """A temporal phrase detected in a query."""

    phrase: str
    kind: str
    guidance: str


@dataclass(frozen=True)
class NormalizedTemporalFact:
    """A relative temporal phrase resolved against an anchor timestamp."""

    phrase: str
    resolved_date: str
    granularity: str
    anchor_date: str


_HINT_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"\byesterday\b", re.IGNORECASE),
        "relative_day",
        "Resolve 'yesterday' as one calendar day before the relevant memory timestamp.",
    ),
    (
        re.compile(r"\blast\s+week\b|\bthe\s+week\s+before\b", re.IGNORECASE),
        "relative_week",
        "Resolve week-relative phrases from the memory timestamp; do not use the record date as the event date.",
    ),
    (
        re.compile(r"\b(previous|last)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE),
        "relative_weekday",
        "Resolve previous weekdays against the relevant memory timestamp.",
    ),
    (
        re.compile(r"\b(two|three|four|\d+)\s+weekends?\s+(before|ago|earlier)\b", re.IGNORECASE),
        "relative_weekend",
        "Resolve weekend offsets by counting complete weekends back from the relevant memory timestamp.",
    ),
    (
        re.compile(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(days?|weeks?|months?|years?)\s+(before|ago|earlier)\b", re.IGNORECASE),
        "relative_offset",
        "Resolve numeric temporal offsets from the relevant memory timestamp.",
    ),
    (
        re.compile(r"\brecently\b|\blately\b", re.IGNORECASE),
        "recent",
        "Treat 'recently' as a request for the latest matching event, not necessarily the newest memory overall.",
    ),
)


def extract_temporal_hints(query: str) -> list[TemporalHint]:
    """Return deterministic temporal hints found in *query*."""

    text = query or ""
    hints: list[TemporalHint] = []
    seen: set[tuple[str, str]] = set()
    for pattern, kind, guidance in _HINT_PATTERNS:
        for match in pattern.finditer(text):
            phrase = match.group(0)
            key = (kind, phrase.lower())
            if key in seen:
                continue
            seen.add(key)
            hints.append(TemporalHint(phrase=phrase, kind=kind, guidance=guidance))
    return hints


def temporal_guidance_for_query(query: str) -> str | None:
    """Format temporal hints for inclusion in a synthesis prompt."""

    hints = extract_temporal_hints(query)
    if not hints:
        return None
    lines = ["Temporal reasoning hints:"]
    for hint in hints:
        lines.append(f"- {hint.phrase}: {hint.guidance}")
    return "\n".join(lines)


def normalize_relative_temporal_facts(
    text: str,
    anchor: datetime | None,
) -> list[NormalizedTemporalFact]:
    """Resolve common LoCoMo relative phrases against a session timestamp."""

    if anchor is None:
        return []
    facts: list[NormalizedTemporalFact] = []
    anchor_date = anchor.date()
    for match in re.finditer(r"\byesterday\b", text, re.IGNORECASE):
        resolved = anchor_date - timedelta(days=1)
        facts.append(_fact(match.group(0), resolved.isoformat(), "day", anchor_date.isoformat()))
    for match in re.finditer(r"\blast\s+week\b|\bthe\s+week\s+before\b", text, re.IGNORECASE):
        resolved = anchor_date - timedelta(days=7)
        facts.append(_fact(match.group(0), resolved.isoformat(), "week", anchor_date.isoformat()))
    for match in re.finditer(
        r"\b(previous|last)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        text,
        re.IGNORECASE,
    ):
        weekday = _WEEKDAY_INDEX[match.group(2).lower()]
        delta = (anchor_date.weekday() - weekday) % 7
        delta = 7 if delta == 0 else delta
        resolved = anchor_date - timedelta(days=delta)
        facts.append(_fact(match.group(0), resolved.isoformat(), "day", anchor_date.isoformat()))
    for match in re.finditer(
        r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(days?|weeks?|months?|years?)\s+(before|ago|earlier)\b",
        text,
        re.IGNORECASE,
    ):
        amount = _number(match.group(1))
        unit = match.group(2).lower()
        days = amount
        granularity = "day"
        if unit.startswith("week"):
            days = amount * 7
            granularity = "week"
        elif unit.startswith("month"):
            days = amount * 30
            granularity = "month"
        elif unit.startswith("year"):
            days = amount * 365
            granularity = "year"
        resolved = anchor_date - timedelta(days=days)
        facts.append(_fact(match.group(0), resolved.isoformat(), granularity, anchor_date.isoformat()))
    return facts


def temporal_metadata(text: str, anchor: datetime | None) -> dict[str, str]:
    """Serialize normalized temporal facts into metadata-safe strings."""

    facts = normalize_relative_temporal_facts(text, anchor)
    if not facts:
        return {}
    return {
        "temporal_anchor": facts[0].anchor_date,
        "temporal_phrase": "|".join(fact.phrase for fact in facts),
        "resolved_date": "|".join(fact.resolved_date for fact in facts),
        "date_granularity": "|".join(fact.granularity for fact in facts),
    }


def query_time_range(query: str, anchor: datetime | None) -> tuple[datetime, datetime] | None:
    """Build a coarse bounded time range for simple relative-date queries."""

    facts = normalize_relative_temporal_facts(query, anchor)
    if not facts:
        return None
    first = facts[0]
    start = datetime.fromisoformat(first.resolved_date)
    if anchor.tzinfo is not None:
        start = start.replace(tzinfo=anchor.tzinfo)
    span = timedelta(days=1 if first.granularity == "day" else 7)
    return start, start + span


def _fact(phrase: str, resolved_date: str, granularity: str, anchor_date: str) -> NormalizedTemporalFact:
    return NormalizedTemporalFact(
        phrase=phrase,
        resolved_date=resolved_date,
        granularity=granularity,
        anchor_date=anchor_date,
    )


_WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_NUMBER_WORDS = {
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


def _number(value: str) -> int:
    return int(value) if value.isdigit() else _NUMBER_WORDS[value.lower()]
