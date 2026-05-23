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

import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from astrocyte.pipeline._json_tolerant import looks_truncated, tolerant_json_loads
from astrocyte.types import Message, PageIndexFact

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider
    from astrocyte.types import PageIndexSection

_logger = logging.getLogger("astrocyte.pipeline.section_fact_extraction")


# M25 — Hindsight-parity fact_type taxonomy.
#
# Previously (M14.1+) had `assistant_statement` as a 6th fact_type to
# preserve assistant phrasing for LME's single-session-assistant
# category. M24 bench showed −2q SSA regression: the inline source-
# chunk pairing made the `assistant_statement` fact text + chunk text
# render redundantly (both contain the assistant utterance), confusing
# the answerer on "what did the assistant say" extraction.
#
# Hindsight's solve (engine/retain/fact_extraction.py lines 150-345 +
# benchmark line 85):
#   1. Binary classification: fact_type ∈ {world, assistant} → mapped
#      to {world, experience} at storage time. Speaker perspective is
#      carried by the speaker field + per-conversation context tag,
#      NOT by a special fact_type bucket.
#   2. Per-conversation perspective tag at extraction time: the prompt
#      tells the LLM "you are the assistant in this conversation" so
#      the extractor uses the right reference frame when classifying
#      first-person utterances.
#
# M25 adopts this pattern:
#   - Drop `assistant_statement` from valid fact_types. Assistant
#     utterances are extracted as `experience` (with speaker='assistant').
#   - Add a perspective-tag preamble to the extraction prompt so the
#     LLM treats the transcript as "a conversation between a user and
#     an AI assistant".
#   - The `speaker` field on MemoryFact preserves the perspective
#     signal for downstream consumers; the answerer renders facts by
#     speaker rather than by fact_type.
#
# Backward compat: legacy rows with fact_type='assistant_statement'
# are accepted on read via the M25 shim in extract_facts_for_section
# (mapped to 'experience' if the LLM emits the legacy tag).
_VALID_FACT_TYPES = {
    "experience",
    "preference",
    "world",
    "plan",
    "opinion",
}

# Legacy fact_types accepted on read but remapped to canonical
# values. Maps the pre-M25 `assistant_statement` to `experience` —
# matches Hindsight's storage-time mapping.
_LEGACY_FACT_TYPE_REMAP = {
    "assistant_statement": "experience",
}
_MAX_FACTS_PER_SECTION = 12  # cap to keep retain cost bounded


_EXTRACT_PROMPT = """\
You are extracting ATOMIC FACTS from one section of a conversation \
transcript. The transcript is a conversation between a USER and an AI \
ASSISTANT — the 'assistant' role IS the AI, the 'user' role is the \
human being talked to. The reader will query these facts directly for \
"how many X", "when did Y", "what does the user prefer for Z", "what \
did the assistant say about W" type questions.

The section's conversation date is ``{session_date}``. Anchor relative \
time phrases ("yesterday", "last week", "3 days ago") against this \
date and output absolute ISO-8601 timestamps.

Output a JSON object with one key, ``facts``, containing an array of \
fact objects. Each fact has:

- "text": SELF-CONTAINED statement that captures WHAT happened AND WHY \
  it matters / context / nuance. (Hindsight `why` parity — the answerer \
  needs the original framing, not just the bare fact.) \
  Include: subject + verb + entities + the REASON / STRENGTH / SCOPE / \
  CONDITIONS. For preferences especially: capture HOW STRONG the \
  preference is, WHY the user prefers it, and any conditions ("for X \
  use case", "compared to Y"). \
  GOOD (preference): "User strongly prefers Sony cameras for product \
  photography because they already own a Sony 24-70mm lens for their \
  candle business; would not consider switching to Canon or Nikon." \
  GOOD (experience): "User visited Dr. Patel for nasal spray prescription \
  on May 5, 2023; this was their third visit after recurring sinus \
  issues from spring allergies." \
  BAD: "Yesterday I went to the doctor" (missing date anchor, no subject) \
  BAD: "User prefers Sony" (missing reason, scope, strength — answerer \
  cannot structure recommendations around bare preference)
- "fact_type": one of:
    - "experience" — something the user did or that happened to them, \
      OR something the assistant said / recommended / explained \
      (use the speaker field to distinguish). Hindsight-parity binary \
      taxonomy: assistant utterances are NOT a separate type; they're \
      experience-typed facts whose speaker is "assistant".
    - "preference" — stable taste, opinion, or choice the user holds
    - "world" — external fact the user mentioned about a non-user entity
    - "plan" — intention, future action, goal
    - "opinion" — value judgment or stance the user expressed
- "speaker": "user" or "assistant" — who stated / did the thing the \
  fact describes. This is the PRIMARY perspective signal. Use \
  speaker="assistant" for any fact that captures what the AI said, \
  recommended, explained, or did in the conversation; speaker="user" \
  for everything the human said / did.
- "occurred_start": ISO-8601 date of when the event happened, or null \
  for non-event facts (preferences, plans, opinions, and most \
  assistant utterances that lack a specific event date)
- "occurred_end": ISO-8601 date for multi-day events, else null
- "entities": array of entity strings. Mix proper nouns ("Dr. Patel", \
  "Nordstrom", "MoMA") and key:value labels for countable categories \
  (``role:doctor``, ``category:trip``, ``event:wedding``, ``expense:$185``).
- "confidence": M27 — float 0.0-1.0 indicating how confident you are \
  in this fact. Use 1.0 for facts explicitly stated by the speaker; \
  0.6-0.8 for facts you inferred from context; 0.4-0.5 for tentative \
  / hedged claims ("might", "maybe"); below 0.5 for facts that are \
  highly speculative. The reader uses this to hedge / abstain on \
  low-confidence facts. Default 0.7 if you're unsure how to score.

Rules:
- Cap at {max_facts} facts per section. Prefer the most-specific facts.
- DO NOT emit "user mentioned X" / "they discussed Y" meta-facts — \
  only the actual atomic facts being discussed.
- DO emit facts for substantive ASSISTANT utterances: recommendations, \
  explanations, answers, advice. Use fact_type="experience" + \
  speaker="assistant". Preserve the assistant's specific substantive \
  content (the recommendation given, the answer provided, the \
  explanation offered) so the reader can quote it back when asked \
  "what did the assistant say about X" / "what did the agent recommend \
  for Y". Skip pure question-asking by the assistant (no extractable \
  content).
- If a fact says "user visited 3 doctors", emit 3 SEPARATE fact rows \
  (one per doctor), not one aggregated fact.
- Skip greetings, small talk, agentic confirmations.
- Generic chit-chat with no specific facts → ``{{"facts": []}}``

COREFERENCE + ALIASING RULES (M29):

These rules make entity strings canonical across sections so cross- \
session link expansion (M27) can stitch "Dr. Patel" in session A to \
"the ENT specialist" in session B without depending on bare-string \
equality. The same person/place/thing should produce the same entity \
token regardless of how the speaker referred to them.

1. PRONOUN RESOLUTION WITHIN SECTION: pronouns ("she", "he", "they", \
   "him", "her", "it") resolve to the most recently named entity in \
   the section. When writing the fact ``text``, substitute the \
   resolved name. \
   GOOD: "Emily said she'd be home late" → fact text: "Emily said she \
   would be home late" (with entities=["Emily"], not entities=["she"]). \
   GOOD: "Dr. Patel called. He confirmed the appointment." → fact \
   text: "Dr. Patel confirmed the appointment" (entities=["Dr. Patel", \
   "role:doctor"]). \
   BAD: emitting "she confirmed..." with no antecedent in the fact \
   text — the fact reads in isolation and the reader has no way to \
   know who "she" is.

2. ALIAS CANONICALIZATION when BOTH a generic reference AND a name \
   appear for the same referent in the section, use the canonical \
   form ``"Name (descriptor)"`` in the entities array (and prefer the \
   name in the fact text). \
   GOOD: "My roommate Emily came by. Emily then left." → \
   entities=["Emily (user's roommate)"]. \
   GOOD: "Dr. Patel walked in. The doctor checked the chart." → \
   entities=["Dr. Patel (role:doctor)", "role:doctor"]. \
   The parenthetical descriptor is what lets a future section that \
   only says "my roommate" or "the doctor" link back to the same \
   referent via the role/relation label.

3. ROLE-BASED ALIASES when only the role appears (no name in this \
   section), still include the role label in entities so cross-section \
   links can find them by role: "the doctor" → include \
   ``"role:doctor"``; "my manager" → include ``"role:manager"``; \
   "my roommate" → include ``"role:roommate"``. The fact text uses the \
   generic reference verbatim. Cross-section link expansion can then \
   join on ``role:doctor`` to surface the named "Dr. Patel" fact from \
   another session.

4. STABLE ENTITY-STRING CONVENTION: \
   - bare ``"Name"`` when the name is unambiguous in the bank \
     ("Dr. Patel", "Emily", "Nordstrom"). \
   - ``"Name (descriptor)"`` when the descriptor disambiguates two \
     referents with the same name OR when both a generic reference \
     and the name appeared in the section (per rule 2). \
   - The descriptor is a short, durable label — a relation \
     ("user's roommate"), a role ("role:doctor"), or a \
     distinguishing attribute ("Emily from Stanford") — not a \
     transient state ("Emily who was tired").

Examples:

Section: "[user] Yesterday I went to Dr. Patel for a nasal spray. \
[assistant] Have you tried the saline rinse I mentioned last visit? \
It clears post-nasal drip too. [user] About 6 months. I prefer his \
clinic over the previous one. The doctor also suggested an antihistamine."
session_date=2023-05-08

Output (note: assistant utterance is fact_type=experience + speaker=assistant; \
"the doctor" in the last user turn refers to Dr. Patel, so its fact uses \
the canonical ``"Dr. Patel (role:doctor)"`` form):
{{"facts": [
  {{"text": "User visited Dr. Patel on May 7, 2023 to get a prescribed nasal spray for ongoing sinus issues.", \
"fact_type": "experience", "speaker": "user", \
"occurred_start": "2023-05-07", "occurred_end": null, \
"entities": ["Dr. Patel (role:doctor)", "nasal spray", "role:doctor"]}},
  {{"text": "User has been seeing Dr. Patel for about 6 months — indicates an established care relationship.", \
"fact_type": "experience", "speaker": "user", \
"occurred_start": null, "occurred_end": null, \
"entities": ["Dr. Patel (role:doctor)", "role:doctor"]}},
  {{"text": "User prefers Dr. Patel's clinic over their previous one — preference is comparative (Patel > previous), implying dissatisfaction with the prior provider.", \
"fact_type": "preference", "speaker": "user", \
"occurred_start": null, "occurred_end": null, \
"entities": ["Dr. Patel (role:doctor)", "role:doctor"]}},
  {{"text": "Dr. Patel also suggested an antihistamine alongside the nasal spray.", \
"fact_type": "experience", "speaker": "user", \
"occurred_start": null, "occurred_end": null, \
"entities": ["Dr. Patel (role:doctor)", "antihistamine", "role:doctor"]}},
  {{"text": "Assistant recommended a saline rinse alongside the nasal spray because it also clears post-nasal drip; offered as complementary, not alternative, treatment.", \
"fact_type": "experience", "speaker": "assistant", \
"occurred_start": null, "occurred_end": null, \
"entities": ["saline rinse", "post-nasal drip"]}}
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
    sess_iso = section.session_date.strftime("%Y-%m-%d") if section.session_date is not None else "unknown"
    msg = _EXTRACT_PROMPT.format(
        session_date=sess_iso,
        section_text=text[:6000],
        max_facts=_MAX_FACTS_PER_SECTION,
    )
    try:
        completion = await provider.complete(
            messages=[Message(role="user", content=msg)],
            model=model,
            max_tokens=2000,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "section_fact_extraction: LLM call failed doc=%s line=%d: %s",
            section.document_id,
            section.line_num,
            exc,
        )
        return []
    # Tolerant parse: handle markdown-fence wrapping / leading-prose noise
    # before giving up. On parse failure, optionally retry once with a
    # stricter system reminder — but skip the retry when the response
    # looks budget-truncated (a retry under the same cap won't help).
    data = tolerant_json_loads(completion.text)
    if data is None and not looks_truncated(completion.text):
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
                max_tokens=2000,
                temperature=0.0,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "section_fact_extraction: retry LLM call failed doc=%s line=%d: %s",
                section.document_id,
                section.line_num,
                exc,
            )
            retry = None
        if retry is not None:
            data = tolerant_json_loads(retry.text)
    if data is None:
        _logger.warning(
            "section_fact_extraction: JSON parse failed doc=%s line=%d",
            section.document_id,
            section.line_num,
        )
        return []
    if not isinstance(data, dict):
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
        # M25 legacy compat: if the LLM emits the pre-M25
        # `assistant_statement` tag (model trained on old prompt OR
        # legacy ingest replay), remap to canonical (`experience` +
        # speaker='assistant') to match Hindsight's storage shape.
        if fact_type in _LEGACY_FACT_TYPE_REMAP:
            fact_type = _LEGACY_FACT_TYPE_REMAP[fact_type]
            # Force speaker='assistant' when remapping from
            # assistant_statement — the perspective signal must survive
            # the type collapse.
            entry.setdefault("speaker", "assistant")
        if not fact_text or fact_type not in _VALID_FACT_TYPES:
            continue
        speaker_raw = entry.get("speaker")
        speaker = str(speaker_raw).strip() or None if isinstance(speaker_raw, str) else None
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
        # M27 — parse confidence score (0.0-1.0).
        # M31b Fix C — DEFAULT to 0.7 when the LLM omits or emits a
        # malformed value. The M27 extraction prompt explicitly tells
        # the LLM "default 0.7 when uncertain"; in practice the LLM
        # often skips the field, leaving confidence_score=None on most
        # facts. That meant Fix 3's confidence-aware abstention rule
        # in the answerer prompt never fired (the answerer can't hedge
        # on confidence it doesn't see). Defaulting at parse-time
        # ensures every newly-extracted fact carries a confidence
        # value the answerer can act on. Out-of-bounds values clamp.
        _DEFAULT_CONF = 0.7
        confidence_score: float = _DEFAULT_CONF
        raw_conf = entry.get("confidence")
        if raw_conf is not None:
            try:
                cf = float(raw_conf)
                if 0.0 <= cf <= 1.0:
                    confidence_score = cf
                elif cf > 1.0:
                    confidence_score = 1.0
                elif cf < 0.0:
                    confidence_score = 0.0
            except (TypeError, ValueError):
                # Keep the default rather than emitting None — Fix 3
                # needs SOMETHING to hedge against.
                confidence_score = _DEFAULT_CONF
        # M31 Fix 4 — resolve relative date phrases in the fact's text
        # to an absolute datetime at retain time. The section's
        # ``session_date`` is the anchor for "last Tuesday" / "3 days
        # ago" style references. Resolution is best-effort: returns
        # ``None`` when no parseable phrase or no anchor. Distinct from
        # ``occurred_start`` (LLM-emitted explicit range) and
        # ``mentioned_at`` (session-level discussion date); see
        # MemoryFact.event_date docstring.
        from astrocyte.pipeline.temporal_resolution import (  # noqa: PLC0415
            resolve_event_date,
        )

        event_date = resolve_event_date(fact_text, section.session_date)

        out.append(
            PageIndexFact(
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
                confidence_score=confidence_score,
                # M27 — `mentioned_at` is the session's date (when the
                # conversation happened), distinct from `occurred_start`
                # (when the event happened). For section-anchored facts
                # we copy section.session_date; top-level facts (no
                # section anchor) leave it None.
                mentioned_at=section.session_date,
                event_date=event_date,  # M31 Fix 4
            )
        )
    return out
