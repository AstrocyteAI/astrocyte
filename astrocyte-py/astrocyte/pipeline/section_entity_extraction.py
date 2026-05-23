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

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider
    from astrocyte.types import PageIndexSection

from astrocyte.pipeline._json_tolerant import looks_truncated, tolerant_json_loads
from astrocyte.types import Message, PageIndexSectionEntity

logger = logging.getLogger("astrocyte.pipeline.section_entity_extraction")


_EXTRACT_PROMPT = """Extract two kinds of entities from the conversation excerpt below.

(A) NAMED ENTITIES — proper nouns the user mentioned:
- People (first names, last names, full names, nicknames)
- Places (cities, countries, neighbourhoods, named buildings)
- Organisations (companies, schools, sports teams, clubs)
- Products (named brands, books, movies, songs, games, foods)
- Notable concepts (named events, named projects, named conditions)

ALIAS CAPTURE for (A): for each PERSON mentioned, emit BOTH the
form used in the excerpt AND any common short-form / nickname /
formal-name variant they are likely to be referred to by. Use general
Western-naming knowledge for the alias mapping:
- "Joanna" → also emit "Jo", "Joey", "Jojo"
- "Robert" → also emit "Rob", "Bob", "Bobby"
- "Elizabeth" → also emit "Liz", "Beth", "Eliza", "Lizzie"
- "Michael" → also emit "Mike", "Mickey"
- "Catherine" / "Katherine" → also emit "Kate", "Cathy", "Katie"
- "William" → also emit "Will", "Bill", "Billy"
- "Jonathan" → also emit "Jon", "Jonny"
- "Christopher" → also emit "Chris"
- "Alexander" → also emit "Alex", "Sandy"
Emit each alias as its OWN entry. Only emit aliases that are PLAUSIBLE
for the person named (don't invent aliases when the name doesn't have
a standard short form). Cap aliases per person at 3.

Do NOT extract for (A):
- Common nouns ("dog", "car", "school" without a name)
- Pronouns
- Dates / times (the temporal index handles those separately)

(B) STRUCTURED LABELS — `key:value` strings that classify what the user \
DID, ENCOUNTERED, or HAS. Use these vocabularies:

- `role:<noun>` — occupational / functional category. Use when the user \
visited / spoke to someone in a role. Examples: `role:doctor`, \
`role:dermatologist`, `role:lawyer`, `role:teacher`, `role:therapist`.
- `category:<noun>` — countable kind of THING the user owns / acquired / \
worked on / consumed. Examples: `category:model_kit`, `category:plant`, \
`category:restaurant`, `category:book`, `category:movie`, \
`category:trip`, `category:doctor_visit`, `category:project`.
- `event:<noun>` — distinct occurrence the user attended / experienced. \
Examples: `event:wedding`, `event:engagement_party`, `event:sale`, \
`event:concert`, `event:road_trip`, `event:job_interview`.
- `expense:<currency_amount>` — money the user spent (when a number is \
mentioned). Examples: `expense:$45`, `expense:$185`, `expense:$2400`.

Rules for (B):
- Use snake_case for the noun. Lowercase.
- Emit ONE label per distinct mention (e.g. user visited 3 doctors → \
emit `role:doctor` 3 times across the relevant sections).
- Match the COUNTABLE category in user questions: "how many doctors?" \
→ `role:doctor`. "How many bikes did I buy?" → `category:bike`. \
"Total spent on bikes?" → `expense:$N`.
- DO NOT invent labels outside the four prefixes above.
- It's fine to emit nothing in (B) if the section is generic chit-chat.

Return ONLY a JSON object with one key, ``entities``, containing an \
array of strings (mixed (A) named entities + (A) aliases + (B) `key:value` \
labels). Cap at 20 entries total; prefer (B) labels when the section \
discusses a countable category, since those drive the wiki recall layer.

Excerpt:
{text}

Output (JSON only):
"""


_MAX_ENTITIES_PER_SECTION = 20


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
        max_tokens=750,
        temperature=0.0,
        response_format={"type": "json_object"},
    )

    # Tolerant parse handles markdown-fence wrapping / leading-prose noise
    # before we give up. On parse failure, retry once with a stricter
    # system reminder unless the response looks budget-truncated (a retry
    # under the same cap won't help).
    parsed = tolerant_json_loads(completion.text)
    if parsed is None and not looks_truncated(completion.text):
        try:
            retry = await provider.complete(
                messages=[
                    Message(
                        role="system",
                        content=(
                            "Return ONLY a valid JSON object. "
                            "No markdown fences. No prose."
                        ),
                    ),
                    Message(role="user", content=msg),
                ],
                model=model,
                max_tokens=750,
                temperature=0.0,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "section_entity_extraction: retry LLM call failed doc=%s line=%d: %s",
                document_id,
                section.line_num,
                exc,
            )
            retry = None
        if retry is not None:
            parsed = tolerant_json_loads(retry.text)
    if not isinstance(parsed, dict):
        logger.warning(
            "section_entity_extraction: JSON parse failed for doc=%s line=%d",
            document_id,
            section.line_num,
        )
        return []
    raw = parsed.get("entities") or []

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
