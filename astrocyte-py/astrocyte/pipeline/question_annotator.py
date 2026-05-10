"""PR2 D.5: LLM-backed question annotator.

Replaces the regex-based ``_extract_question_entities`` heuristic with
a single LLM call that returns both:

1. **Entities** (proper + common nouns the question hinges on) — fed to
   the entity strategy. Catches lowercase nouns the regex misses
   ("pendant", "obesity", "dog treats", "taekwondo", "France"), which
   the open-domain failure analysis identified as the pivot for
   specific-fact questions.

2. **Date range** (start + end, anchored against a ``reference_date``) —
   fed to the temporal strategy as a *narrow* window. PR2-D.1-4 LME
   temporal-reasoning was 0% because we passed the temporal strategy
   the full conversation date range; without question-side date parsing
   it had nothing to filter on. This module gives it a real window.

One LLM call per question (~$0.0002 at gpt-4o-mini prices). Both
fields are optional — the orchestrator handles missing entities or
date_range gracefully.

See:
- docs/_design/recall.md §6 (recall pipeline, mode classifier slot)
- PR2-D.1-4 LME gate analysis (temporal-reasoning 0% root cause)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider

from astrocyte.types import Message

logger = logging.getLogger("astrocyte.pipeline.question_annotator")


@dataclass
class QuestionAnnotation:
    """One question's parsed structure for the Tier-2 retrieval driver.

    Both fields are optional. ``entities=[]`` skips the entity
    strategy; ``date_range=None`` skips the temporal strategy."""

    entities: list[str]
    date_range: tuple[datetime, datetime] | None


_PROMPT = """You are an analyst extracting search keys from a user's question about a long conversation transcript.

Reference date (treat as "today" when resolving relative phrases): {reference_date}

Return ONLY a JSON object with these keys:

- "entities": array of strings — names, places, things, concepts, activities the question hinges on. Include BOTH proper nouns (people, places, brands) AND concrete common nouns (objects, activities, conditions). Skip stopwords (the, a, did, what, when, etc.) and tense markers. Aim for 1-6 entries.

- "date_range": object with ISO-8601 "start" and "end" date strings, OR {{"start": null, "end": null}} when the question has no temporal anchor. Use the reference date to resolve relative phrases like "last week" or "two months ago".

Examples (reference_date "22 October, 2023"):

Q: "What did Caroline research?"
→ {{"entities": ["Caroline", "research"], "date_range": {{"start": null, "end": null}}}}

Q: "In what country did Jolene's mother buy her the pendant?"
→ {{"entities": ["Jolene", "mother", "pendant", "country"], "date_range": {{"start": null, "end": null}}}}

Q: "What are John's suspected health problems?"
→ {{"entities": ["John", "health problems"], "date_range": {{"start": null, "end": null}}}}

Q: "What did Caroline say in May 2023?"
→ {{"entities": ["Caroline"], "date_range": {{"start": "2023-05-01", "end": "2023-05-31"}}}}

Q: "Who did Maria have dinner with on May 3, 2023?"
→ {{"entities": ["Maria", "dinner"], "date_range": {{"start": "2023-05-03", "end": "2023-05-03"}}}}

Q: "What was Caroline doing two months ago?"
→ {{"entities": ["Caroline"], "date_range": {{"start": "2023-08-01", "end": "2023-08-31"}}}}

Q: "What temporary job did Jon take to cover expenses?"
→ {{"entities": ["Jon", "temporary job", "expenses"], "date_range": {{"start": null, "end": null}}}}

Question: {question}
Output (JSON only):"""


async def annotate_question(
    provider: "LLMProvider",
    question: str,
    *,
    reference_date: str | None = None,
    model: str | None = None,
) -> QuestionAnnotation:
    """Single LLM call that returns entities + date_range.

    ``reference_date`` is the human-readable date string from the
    conv_tree (e.g. "22 October, 2023"). When None, the prompt uses a
    placeholder; date phrases referencing "today" can't resolve, but
    explicit dates still parse.

    Returns ``QuestionAnnotation(entities=[], date_range=None)`` on
    LLM failure or parse error — the orchestrator degrades gracefully
    (just skips the entity / temporal strategies for this question).
    """
    if not question.strip():
        return QuestionAnnotation(entities=[], date_range=None)

    prompt = _PROMPT.format(
        question=question,
        reference_date=reference_date or "(unknown)",
    )

    try:
        completion = await provider.complete(
            messages=[Message(role="user", content=prompt)],
            model=model,
            max_tokens=200,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001 — annotator failure shouldn't tank a question
        logger.warning(
            "annotate_question: LLM call failed for q=%r: %s: %s",
            question[:80], type(exc).__name__, exc,
        )
        return QuestionAnnotation(entities=[], date_range=None)

    try:
        parsed = json.loads(completion.text)
    except json.JSONDecodeError:
        logger.warning(
            "annotate_question: JSON parse failed for q=%r; raw=%r",
            question[:80], completion.text[:120],
        )
        return QuestionAnnotation(entities=[], date_range=None)

    raw_entities = parsed.get("entities") or []
    entities: list[str] = []
    seen: set[str] = set()
    for e in raw_entities:
        if not isinstance(e, str):
            continue
        clean = e.strip()
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        entities.append(clean)

    date_range = _parse_iso_range(parsed.get("date_range"))
    return QuestionAnnotation(entities=entities, date_range=date_range)


def _parse_iso_range(raw) -> tuple[datetime, datetime] | None:
    """Coerce ``{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}`` → tz-aware
    UTC datetime tuple. Returns ``None`` on missing / malformed input."""
    if not isinstance(raw, dict):
        return None
    start_s = raw.get("start")
    end_s = raw.get("end")
    if not start_s or not end_s:
        return None
    try:
        start = datetime.strptime(start_s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(end_s, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc,
        )
    except (ValueError, TypeError):
        return None
    if end < start:
        # LLM occasionally swaps; tolerate.
        start, end = end, start
    return (start, end)
