"""M11.1 — structured event-time extraction per section.

Mirrors Hindsight's per-fact ``occurred_start`` / ``occurred_end``
columns on ``memory_units``. At retain time, for each section, ask
the LLM to identify the most-prominent event the section describes
and emit ISO-8601 start (and optional end) timestamps.

Why this differs from ``session_date`` (already on every section):

- ``session_date`` is when the conversation session HAPPENED
  (e.g. May 8, 2023 — when the user typed the message)
- ``occurred_start`` is when the discussed EVENT happened
  (e.g. May 7 — "yesterday I went to the doctor")

LME temporal-reasoning failures all share the same shape: the picker
finds the right SESSION but the synth uses the SESSION date instead
of the EVENT date. With ``occurred_start`` populated,
:func:`~astrocyte.pipeline.temporal_arithmetic.find_event_date` can
return the canonical event date directly.

Relative phrases ("yesterday", "last week", "3 days ago") are anchored
against the section's ``session_date`` at extraction time so the
output is always an absolute ISO timestamp.

See:
- ``docs/_design/recall.md`` §13 (M10 close-out) + §14 (M11 plan)
- ``hindsight/hindsight-api-slim/hindsight_api/engine/retain/fact_extraction.py``
  for the canonical Hindsight pattern at memory_unit grain.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from astrocyte.types import Message

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider
    from astrocyte.types import PageIndexSection

_logger = logging.getLogger("astrocyte.pipeline.section_event_extraction")


_EXTRACT_PROMPT = """\
You are extracting the structured EVENT TIME from one section of a \
conversation transcript. Your output drives query-time temporal \
arithmetic — questions like "how many weeks ago did I visit the \
doctor?" need the canonical date of the doctor visit, NOT the date \
the user mentioned it.

The section's conversation date is ``{session_date}``. Anchor any \
relative time phrase ("yesterday", "last week", "3 days ago", "last \
month") against THIS date. Output absolute ISO-8601 timestamps.

Output a JSON object with EXACTLY these fields:
- "occurred_start": ISO-8601 timestamp of when the most-prominent \
discussed event began. ``null`` if the section is generic chit-chat \
with no specific event.
- "occurred_end": ISO-8601 timestamp of when that event ended. \
``null`` if it's a single-day or instantaneous event.
- "event_description": 3-6 word description of the event (for \
provenance / debugging). ``null`` if no specific event.

Rules:
- "Yesterday I went to the doctor" with session_date=2023-05-08 → \
  ``{{"occurred_start": "2023-05-07", "occurred_end": null, \
"event_description": "doctor visit"}}``
- "We had a wedding two Saturdays ago" with session_date=2023-05-15 \
  → ``{{"occurred_start": "2023-05-06", "occurred_end": null, \
"event_description": "wedding"}}``
- "Spent last weekend camping" with session_date=2023-05-22 \
  (Monday) → ``{{"occurred_start": "2023-05-20", "occurred_end": \
"2023-05-21", "event_description": "weekend camping"}}``
- "Trip from May 3-15" → ``{{"occurred_start": "2023-05-03", \
"occurred_end": "2023-05-15", "event_description": "trip"}}``
- Generic chit-chat ("How are you today?", recipe discussion with \
no specific past event) → ``{{"occurred_start": null, "occurred_end": \
null, "event_description": null}}``

When the section discusses MULTIPLE events, pick the most-prominent \
one (the one the user is asking about / spent the most time on). Do \
NOT try to list multiple events — one per section.

If a relative phrase is ambiguous ("recently", "a while back"), \
return ``null`` rather than guessing.

OUTPUT MUST BE VALID JSON. No prose around it.

Section content:
{section_text}
"""


def _parse_iso_date(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        # Accept both "2023-05-07" and "2023-05-07T12:00:00" forms.
        if "T" in s or ":" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except (ValueError, TypeError) as exc:
        _logger.debug("section_event_extraction: bad ISO date %r: %s", s, exc)
        return None


async def extract_event_date_for_section(
    provider: "LLMProvider",
    section: "PageIndexSection",
    section_text: str,
    *,
    model: str | None = None,
) -> tuple[datetime | None, datetime | None]:
    """One LLM call → ``(occurred_start, occurred_end)`` for this section.

    Returns ``(None, None)`` when:
    - the section is generic chit-chat with no specific event
    - the LLM output fails to parse
    - the LLM declines to commit a date (ambiguous phrase)

    Caller persists the returned dates via
    :meth:`PageIndexStore.save_section_event_dates`.
    """
    text = section_text.strip()
    if not text:
        return None, None
    sess_iso = (
        section.session_date.strftime("%Y-%m-%d")
        if section.session_date is not None
        else "unknown"
    )
    msg = _EXTRACT_PROMPT.format(
        session_date=sess_iso,
        section_text=text[:6000],  # ~1500 tokens cap
    )
    try:
        completion = await provider.complete(
            messages=[Message(role="user", content=msg)],
            model=model,
            max_tokens=200,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "section_event_extraction: LLM call failed doc=%s line=%d: %s",
            section.document_id, section.line_num, exc,
        )
        return None, None
    try:
        data = json.loads(completion.text)
    except json.JSONDecodeError:
        _logger.warning(
            "section_event_extraction: JSON parse failed doc=%s line=%d text=%r",
            section.document_id, section.line_num, completion.text[:200],
        )
        return None, None
    start = _parse_iso_date(data.get("occurred_start"))
    end = _parse_iso_date(data.get("occurred_end"))
    return start, end
