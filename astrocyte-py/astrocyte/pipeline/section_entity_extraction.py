"""PR2 commit A: per-section entity extraction for section recall.

Extracts named entities (people, places, organisations, products,
notable concepts) from a PageIndex tree section's text. Output rows go
into ``astrocyte_pi_section_entities`` and become the entity-lookup
strategy's index in PR2 commit B (Hindsight's CTE pattern at section
grain).

Why per-section: the picker can't route on "Caroline" if the tree
summary says "they discussed personal experiences". Stamping
``entities`` rows means a question that mentions Caroline gets routed
to *every* section mentioning her in <100ms, deterministically.

Cost: one LLM call per section. For LoCoMo (~30 sections per
conversation × 10 conversations) that's ~300 calls one-time at retain.
At gpt-4o-mini prices, ~$0.001 per section → ~$3 to build entity index
across the full LoCoMo dataset.

Design notes:
- We ask the LLM to return a JSON array of strings (no schema enforcement
  at PR2-A scope; the picker is robust to noisy entity rows).
- De-duplicate case-insensitively on the way out (``Caroline`` and
  ``caroline`` collapse to one row).
- Cap at 15 entities per section — pathological extractions (lyric
  quotations, recipe ingredients) shouldn't blow up the index. The cap
  is loose; ``ix_pi_section_entities_name`` handles fanout via
  Hindsight's LATERAL pattern in PR2 commit B.
- We DO NOT try to canonicalise across sections at PR2-A. "Jon" and
  "Jonathan Smith" stay separate rows. PR2 commit D adds a per-bank
  entity-resolution pass if the bench shows it matters.

See:
  - docs/_design/recall.md §5 (retain pipeline) and §8.1
  - docs/_design/adr/adr-007-pageindex-tree-as-section-primitive.md
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider
    from astrocyte.types import PageIndexSection

from astrocyte.types import Message, PageIndexSectionEntity

logger = logging.getLogger("astrocyte.pipeline.section_entity_extraction")


_EXTRACT_PROMPT = """Extract the named entities from the conversation excerpt below.

Named entities include:
- People (first names, last names, full names, nicknames)
- Places (cities, countries, neighbourhoods, named buildings)
- Organisations (companies, schools, sports teams, clubs)
- Products (named brands, books, movies, songs, games, foods)
- Notable concepts (named events, named projects, named conditions)

Do NOT extract:
- Common nouns ("dog", "car", "school" without a name)
- Pronouns
- Dates / times (the temporal index handles those separately)

Return ONLY a JSON object with one key, ``entities``, containing an array of
entity-name strings. Strings should be the canonical form as written in the text
(don't normalise case). Cap at 15 most-mentioned entities; the index is sized
for breadth, not depth.

Excerpt:
{text}

Output (JSON only):
"""


_MAX_ENTITIES_PER_SECTION = 15


async def extract_entities_for_section(
    provider: "LLMProvider",
    document_id: str,
    section: "PageIndexSection",
    section_text: str,
    *,
    model: str | None = None,
) -> list[PageIndexSectionEntity]:
    """One LLM call → up to 15 ``PageIndexSectionEntity`` rows.

    ``section_text`` is the sliced markdown for the section (caller
    extracts via ``_slice_section_around_line`` from the bench). We pass
    it rather than re-slicing here so the bench can batch the slicing
    logic in one place.

    Returns an empty list on parse failure (logged) — the picker
    degrades gracefully when entity rows are missing for a section.
    """
    if not section_text.strip():
        return []

    msg = _EXTRACT_PROMPT.format(text=section_text[:6000])  # 6K char cap = ~1500 tokens
    completion = await provider.complete(
        messages=[Message(role="user", content=msg)],
        model=model,
        max_tokens=400,
        temperature=0.0,
        response_format={"type": "json_object"},
    )

    try:
        parsed = json.loads(completion.text)
        raw = parsed.get("entities") or []
    except json.JSONDecodeError:
        logger.warning(
            "section_entity_extraction: JSON parse failed for doc=%s line=%d",
            document_id, section.line_num,
        )
        return []

    # Dedupe case-insensitively, preserve first-seen casing, cap.
    seen: set[str] = set()
    out: list[PageIndexSectionEntity] = []
    for raw_name in raw:
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            PageIndexSectionEntity(
                document_id=document_id,
                line_num=section.line_num,
                entity_name=name,
            )
        )
        if len(out) >= _MAX_ENTITIES_PER_SECTION:
            break
    return out
