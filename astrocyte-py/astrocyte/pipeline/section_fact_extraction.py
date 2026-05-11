"""M12.1 — per-section fact extraction.

Each section's raw text is exploded into a list of atomic facts via
one LLM call. Each fact carries:

- ``text``: self-contained statement ("User visited Dr. Patel on May 5")
- ``fact_type``: ``experience | preference | world | plan | opinion``
- ``speaker``: ``user | assistant``
- ``occurred_start`` / ``occurred_end``: anchored to ``session_date`` for
  relative phrases ("yesterday" → session - 1)
- ``entities``: proper nouns + key:value labels from the M10.2 vocab
  (``role:doctor``, ``category:trip``, ``event:wedding``, ``expense:$N``)

Sections remain the picker's navigation primitive; facts are the
precision grain queried by reflect tools (counting, temporal, entity
lookups). Mirrors Hindsight's ``memory_units`` schema on top of the
PageIndex tree.

See:
- ``docs/_design/recall.md`` §14 (M12 plan)
- ``hindsight-api-slim/hindsight_api/engine/retain/fact_extraction.py``
  for the canonical Hindsight pattern.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from astrocyte.types import Message, PageIndexFact

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider
    from astrocyte.types import PageIndexSection

_logger = logging.getLogger("astrocyte.pipeline.section_fact_extraction")


_VALID_FACT_TYPES = {"experience", "preference", "world", "plan", "opinion"}
_MAX_FACTS_PER_SECTION = 12  # cap to keep retain cost bounded


_EXTRACT_PROMPT = """\
You are extracting ATOMIC FACTS from one section of a conversation \
transcript. Each fact = ONE self-contained statement. The reader will \
query these facts directly for "how many X", "when did Y", "what does \
the user prefer for Z" type questions.

The section's conversation date is ``{session_date}``. Anchor relative \
time phrases ("yesterday", "last week", "3 days ago") against this \
date and output absolute ISO-8601 timestamps.

Output a JSON object with one key, ``facts``, containing an array of \
fact objects. Each fact has:

- "text": SELF-CONTAINED statement (include subject + verb + entities). \
  GOOD: "User visited Dr. Patel for nasal spray prescription on May 5, \
  2023." \
  BAD: "Yesterday I went to the doctor" (missing date anchor, no subject)
- "fact_type": one of:
    - "experience" — something the user did or that happened to them
    - "preference" — stable taste, opinion, or choice the user holds
    - "world" — external fact about a non-user entity
    - "plan" — intention, future action, goal
    - "opinion" — value judgment or stance the user expressed
- "speaker": "user" or "assistant" — who stated this fact
- "occurred_start": ISO-8601 date of when the event happened, or null \
  for non-event facts (preferences, plans, opinions)
- "occurred_end": ISO-8601 date for multi-day events, else null
- "entities": array of entity strings. Mix proper nouns ("Dr. Patel", \
  "Nordstrom", "MoMA") and key:value labels for countable categories \
  (``role:doctor``, ``category:trip``, ``event:wedding``, ``expense:$185``).

Rules:
- Cap at {max_facts} facts per section. Prefer the most-specific facts.
- DO NOT emit "user mentioned X" / "they discussed Y" meta-facts — \
  only the actual atomic facts being discussed.
- If a fact says "user visited 3 doctors", emit 3 SEPARATE fact rows \
  (one per doctor), not one aggregated fact.
- Skip greetings, small talk, agentic confirmations.
- Generic chit-chat with no specific facts → ``{{"facts": []}}``

Examples:

Section: "[user] Yesterday I went to Dr. Patel for a nasal spray. \
[assistant] How long have you been seeing Dr. Patel? [user] About 6 \
months. I prefer his clinic over the previous one."
session_date=2023-05-08

Output:
{{"facts": [
  {{"text": "User visited Dr. Patel for a nasal spray on May 7, 2023.", \
"fact_type": "experience", "speaker": "user", \
"occurred_start": "2023-05-07", "occurred_end": null, \
"entities": ["Dr. Patel", "nasal spray", "role:doctor"]}},
  {{"text": "User has been seeing Dr. Patel for about 6 months.", \
"fact_type": "experience", "speaker": "user", \
"occurred_start": null, "occurred_end": null, \
"entities": ["Dr. Patel", "role:doctor"]}},
  {{"text": "User prefers Dr. Patel's clinic over their previous one.", \
"fact_type": "preference", "speaker": "user", \
"occurred_start": null, "occurred_end": null, \
"entities": ["Dr. Patel", "role:doctor"]}}
]}}

OUTPUT MUST BE VALID JSON. No prose around it.

Section content:
{section_text}
"""


def _parse_iso_date(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        if "T" in s or ":" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


async def extract_facts_for_section(
    provider: "LLMProvider",
    section: "PageIndexSection",
    section_text: str,
    *,
    bank_id: str,
    model: str | None = None,
) -> list[PageIndexFact]:
    """One LLM call → up to ``_MAX_FACTS_PER_SECTION`` atomic facts.

    Returns ``[]`` when:
    - section is generic chit-chat with no specific facts
    - LLM output fails to parse
    - all candidate facts violated schema (bad fact_type, missing text)

    Caller persists the returned facts via
    :meth:`PageIndexStore.save_facts`.
    """
    text = section_text.strip()
    if not text:
        return []
    sess_iso = (
        section.session_date.strftime("%Y-%m-%d")
        if section.session_date is not None
        else "unknown"
    )
    msg = _EXTRACT_PROMPT.format(
        session_date=sess_iso,
        section_text=text[:6000],
        max_facts=_MAX_FACTS_PER_SECTION,
    )
    try:
        completion = await provider.complete(
            messages=[Message(role="user", content=msg)],
            model=model,
            max_tokens=1500,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "section_fact_extraction: LLM call failed doc=%s line=%d: %s",
            section.document_id, section.line_num, exc,
        )
        return []
    try:
        data = json.loads(completion.text)
    except json.JSONDecodeError:
        _logger.warning(
            "section_fact_extraction: JSON parse failed doc=%s line=%d",
            section.document_id, section.line_num,
        )
        return []
    raw = data.get("facts") or []
    if not isinstance(raw, list):
        return []

    out: list[PageIndexFact] = []
    for entry in raw[:_MAX_FACTS_PER_SECTION]:
        if not isinstance(entry, dict):
            continue
        fact_text = str(entry.get("text", "")).strip()
        fact_type = str(entry.get("fact_type", "")).strip()
        if not fact_text or fact_type not in _VALID_FACT_TYPES:
            continue
        speaker_raw = entry.get("speaker")
        speaker = (
            str(speaker_raw).strip() or None
            if isinstance(speaker_raw, str)
            else None
        )
        if speaker is not None and speaker not in {"user", "assistant"}:
            speaker = None
        ents_raw = entry.get("entities") or []
        if not isinstance(ents_raw, list):
            ents_raw = []
        entities = [str(e).strip() for e in ents_raw if isinstance(e, str) and str(e).strip()]
        # Dedupe entities case-insensitively, preserve first-seen casing
        seen: set[str] = set()
        deduped: list[str] = []
        for e in entities:
            k = e.casefold()
            if k in seen:
                continue
            seen.add(k)
            deduped.append(e)
        out.append(PageIndexFact(
            id=str(uuid.uuid4()),
            bank_id=bank_id,
            document_id=section.document_id,
            line_num=section.line_num,
            text=fact_text,
            fact_type=fact_type,
            speaker=speaker,
            occurred_start=_parse_iso_date(entry.get("occurred_start")),
            occurred_end=_parse_iso_date(entry.get("occurred_end")),
            entities=deduped,
            embedding=None,  # embeddings batched separately at retain time
        ))
    return out
