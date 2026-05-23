"""M22 — Hindsight-parity answerer prompt + structured context (Gaps 1, 2, 3, 4).

Ports the Hindsight bench harness's answerer prompt and context shape into
the Mem0 bench wrapper so the answerer sees:

- **Gap 1** — directive prompt with explicit counting / disambiguation /
  date-arithmetic / recommendation rules (~85 lines), modelled on
  ``hindsight-dev/benchmarks/longmemeval/longmemeval_benchmark.py:142-340``.
- **Gap 2** — facts paired with their source-chunk text so the answerer
  reads the raw conversation alongside the extracted summary. We group
  fact-grain candidates with their adjacent section-grain candidate
  (sharing ``line_num``) and surface both under one Fact entry.
- **Gap 3** — observations + mental models rendered as **separate**
  prompt sections (``=== Entity Observations ===`` and
  ``=== Mental Models ===``) instead of competing in the same flat list
  with raw memories.
- **Gap 4** — per-question-type prompt routing. The directive prompt
  already contains category-specific blocks (recommendation,
  counting, temporal, etc.); we also inject a question-type-aware
  preface so the answerer knows up front which block applies.

Gated by ``ASTROCYTE_M22_HINDSIGHT_ANSWERER=1``. Default OFF so existing
benches are byte-identical until the flag flips.

Installation: ``run_lme.py`` / ``run_locomo.py`` call
:func:`maybe_install_hindsight_answerer_patch` which monkey-patches
``get_answer_generation_prompt`` in the upstream
``benchmarks/{lme,locomo}/prompts.py`` modules. The signature is
preserved so the bench's ``process_question`` continues to call it
unchanged.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


def is_enabled() -> bool:
    """True when ``ASTROCYTE_M22_HINDSIGHT_ANSWERER`` is set to a truthy value.

    Default OFF — existing M19/M21 default behaviour preserved. Set
    ``=1`` / ``=true`` / ``=yes`` to enable.
    """
    val = os.environ.get("ASTROCYTE_M22_HINDSIGHT_ANSWERER", "").lower()
    return val in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Hindsight-style structured context format (Gap 1 + Gap 2 + Gap 3)
# ---------------------------------------------------------------------------


def _to_human_date(ts: str | None) -> str | None:
    """Best-effort ISO-or-other → 'YYYY-MM-DD' display."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return ts


def _parse_to_date(ts: Any) -> datetime | None:
    """Parse an ISO-string or datetime into a tz-aware datetime, else None."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _format_relative_delta(
    fact_date_str: str | None,
    reference_date_str: str | None,
) -> str | None:
    """M31d Fix D — pre-compute a human-readable relative time hint
    so the answerer doesn't have to do date math (which gpt-4o-mini
    is unreliable at). Returns ``"(4 weeks ago)"`` / ``"(3 days ago)"``
    / ``"(2 months ago)"`` / ``"(2 years ago)"`` or ``None``.

    Diagnostic that motivated this: 4 of 10 v015d TR failures had
    correct date facts retrieved but wrong arithmetic (e.g. answered
    "5 weeks ago" when truth was "4 weeks ago", off by ~7 days each
    time). The LLM consistently miscomputes (B - A) / 7. Pre-rendering
    the delta as text turns the math problem into a read problem.

    Strips timezone awareness for cross-tz robustness (LME questions
    and dataset facts may use different tz representations).
    """
    fact_dt = _parse_to_date(fact_date_str)
    ref_dt = _parse_to_date(reference_date_str)
    if fact_dt is None or ref_dt is None:
        return None
    # Normalize: both dates aware-or-naive consistently. Use date()
    # comparison to dodge sub-day timezone drift.
    try:
        delta_days = (ref_dt.date() - fact_dt.date()).days
    except (TypeError, ValueError):
        return None
    if delta_days <= 0:
        return None  # future date or same day — don't add a hint
    if delta_days < 7:
        return f"({delta_days} days ago)" if delta_days != 1 else "(yesterday)"
    if delta_days < 35:
        weeks = round(delta_days / 7)
        return f"({weeks} week{'s' if weeks != 1 else ''} ago)"
    if delta_days < 365:
        months = round(delta_days / 30)
        return f"({months} month{'s' if months != 1 else ''} ago)"
    years = round(delta_days / 365)
    return f"({years} year{'s' if years != 1 else ''} ago)"


def _truncate(text: str, limit: int = 1000) -> str:
    """Hindsight truncates chunks at 1000 chars in
    ``_format_context_structured`` (line 226 of their LME benchmark).
    Apply the same cap here so the answerer's context length stays
    bounded on long sessions.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _group_facts_with_chunks(
    search_results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ``search_results`` into (facts, standalone_chunks).

    The bench harness emits a flat candidate list mixing four grains
    (set in ``astrocyte_client.search`` post-rerank):

    - ``grain=fact``    — atomic LLM-extracted facts. Post-M24 these
      carry inline ``chunk_id`` + ``source_chunk`` text (the
      Hindsight-parity pairing). They're rendered as
      ``Fact N: ... Source: "..."`` blocks.
    - ``grain=section`` — raw conversation excerpts around a line_num
      that were NOT already paired into a fact above. These show up
      in the ``=== Source Chunks ===`` fallback section so we don't
      drop them.
    - ``grain=raw_chunk`` — verbatim conversation excerpts at very
      high score (M14.7 anchor-injection path). Same fallback bucket.
    - ``grain=wiki``    — per-doc compiled observations. Same fallback
      bucket OR redirected to entity observations when that section
      isn't already populated by the bench's
      ``list_observations_for_bench`` helper.

    The M24 architectural fix made fact→chunk pairing direct: facts
    with ``source_chunk`` set in their candidate dict get rendered
    inline. Sections that DON'T already appear as a paired chunk
    under some fact are kept in the standalone bucket so the
    answerer can still see them — same data, just no longer split
    into two competing rendering layers.
    """
    facts: list[dict[str, Any]] = []
    standalone_chunks: list[dict[str, Any]] = []
    # Track chunk_ids that have been paired with at least one fact so
    # we can de-dupe section/raw_chunk entries that would otherwise
    # appear twice (once inline under a fact, once in the fallback).
    paired_chunk_ids: set[str] = set()
    for r in search_results:
        meta = r.get("metadata") or {}
        grain = meta.get("grain", "fact")
        if grain in ("section", "raw_chunk", "wiki"):
            standalone_chunks.append(r)
        else:
            facts.append(r)
            cid = r.get("chunk_id")
            if cid:
                paired_chunk_ids.add(cid)

    # Drop standalone chunks whose ID matches a chunk already paired
    # with a fact above. The section entry's id format is
    # ``section:<document_id>:<line_num>``; the paired chunk_id is
    # ``<document_id>:<line_num>``. Suffix-match is sufficient.
    if paired_chunk_ids:
        filtered: list[dict[str, Any]] = []
        for c in standalone_chunks:
            entry_id = str(c.get("id", ""))
            # Strip the "section:" / "raw_chunk:" / etc. prefix.
            tail = entry_id.split(":", 1)[1] if ":" in entry_id else entry_id
            if tail in paired_chunk_ids:
                continue
            filtered.append(c)
        standalone_chunks = filtered

    return facts, standalone_chunks


def format_context_structured(
    search_results: list[dict[str, Any]],
    *,
    observations: list[dict[str, Any]] | None = None,
    mental_models: list[dict[str, Any]] | None = None,
    reference_date: str | None = None,
) -> str:
    """Render search results in Hindsight's structured shape.

    Mirrors ``LongMemEvalAnswerGenerator._format_context_structured``
    in spirit but adapted to our four-grain candidate pool. Output:

      Fact 1: <text>
      When: <created_at>
      Source: "<chunk text>"
      ---
      Fact 2: ...

      === Source Chunks ===
      (chunk 1 text)
      ---
      (chunk 2 text)

      === Entity Observations ===     ← Gap 3
      - <observation text>  [trend, proof_count=N]

      === Mental Models ===            ← Gap 3
      ## <title> (kind=<kind>)
      <rendered content>
    """
    facts, standalone_chunks = _group_facts_with_chunks(search_results)
    if not facts and not standalone_chunks and not observations and not mental_models:
        return "No memories found."

    parts: list[str] = []

    # Facts — Hindsight-parity rendering with inline source chunk
    # (M24 architectural fix). Each fact's ``source_chunk`` field is
    # populated by ``astrocyte_client.search`` from the section text
    # at the fact's ``(document_id, line_num)`` anchor.
    for i, fact in enumerate(facts, 1):
        entry: list[str] = []
        memory = fact.get("memory") or fact.get("text") or ""
        entry.append(f"Fact {i}: {memory}")
        # M28 Workstream B — render dual timestamps when both occurred
        # (``created_at``) and mentioned (``mentioned_at``) are present
        # so the answerer can disambiguate "when did X happen" from
        # "when did the user tell us X happened". Falls back to the
        # single-date form when only one side is available.
        #
        # M31d Fix D — append a pre-computed "(N weeks ago)" hint
        # against the question's reference_date. The LLM is unreliable
        # at date math (4/10 v015d TR failures were arithmetic errors).
        # Pre-rendering turns the math into a read.
        occurred_raw = fact.get("created_at")
        mentioned_raw = fact.get("mentioned_at")
        occurred = _to_human_date(occurred_raw)
        mentioned = _to_human_date(mentioned_raw)
        if occurred and mentioned and occurred != mentioned:
            line = f"When: occurred: {occurred} | mentioned: {mentioned}"
            occ_delta = _format_relative_delta(occurred_raw, reference_date)
            men_delta = _format_relative_delta(mentioned_raw, reference_date)
            if occ_delta or men_delta:
                # Show the delta on the most-relevant side (occurred is
                # what TR questions ask about).
                line += f"  occurred {occ_delta or ''}".rstrip()
            entry.append(line)
        elif occurred:
            line = f"When: {occurred}"
            delta = _format_relative_delta(occurred_raw, reference_date)
            if delta:
                line += f" {delta}"
            entry.append(line)
        elif mentioned:
            line = f"When: {mentioned}"
            delta = _format_relative_delta(mentioned_raw, reference_date)
            if delta:
                line += f" {delta}"
            entry.append(line)
        # M31 Fix 4 — resolved absolute event date. Distinct from
        # ``occurred`` (LLM-emitted explicit range) — ``event_date``
        # is the single deterministically-resolved date that "last
        # Tuesday" / "3 days ago" maps to. When present, the answerer
        # should prefer this over doing relative-phrase math at
        # query time. See the M31 confidence + event_date guidance
        # in ``_SHARED_PRELUDE``.
        event_date_raw = fact.get("event_date")
        event_date = _to_human_date(event_date_raw)
        if event_date:
            line = f"Event date: {event_date}"
            delta = _format_relative_delta(event_date_raw, reference_date)
            if delta:
                line += f" {delta}"
            entry.append(line)
        # M28 Workstream B — surface M27 per-fact confidence so the
        # answerer can hedge or abstain on low-confidence claims. See
        # the prelude's confidence-interpretation guidance.
        confidence = fact.get("confidence_score")
        if confidence is not None:
            try:
                entry.append(f"Confidence: {float(confidence):.2f}")
            except (TypeError, ValueError):
                pass
        source_chunk = fact.get("source_chunk")
        if source_chunk:
            chunk_truncated = _truncate(source_chunk, 1200)
            entry.append(f'Source chunk:\n  "{chunk_truncated}"')
        parts.append("\n".join(entry))

    # Standalone chunks — sections / wikis / raw_chunks that weren't
    # paired into a fact above (e.g. section_recall surfaced a chunk
    # that no fact_recall fact resides in). Rendered as a fallback
    # section so we don't drop them; the answerer can still cross-
    # reference if needed.
    if standalone_chunks:
        chunk_section: list[str] = ["=== Additional Source Chunks (unpaired) ==="]
        for c in standalone_chunks:
            text = c.get("memory") or c.get("text") or ""
            text = _truncate(text, 1000)
            chunk_section.append(text)
        parts.append("\n\n".join(chunk_section))

    # Entity observations (Gap 3)
    if observations:
        obs_section: list[str] = ["=== Entity Observations ==="]
        for obs in observations:
            txt = obs.get("text", "").strip()
            if not txt:
                continue
            trend = obs.get("trend", "")
            proof = obs.get("proof_count", 1)
            suffix_bits = []
            if trend:
                suffix_bits.append(trend)
            if proof and int(proof) > 1:
                suffix_bits.append(f"proof_count={proof}")
            suffix = f"  [{', '.join(suffix_bits)}]" if suffix_bits else ""
            obs_section.append(f"- {txt}{suffix}")
        if len(obs_section) > 1:
            parts.append("\n".join(obs_section))

    # Mental models (Gap 3)
    if mental_models:
        mm_section: list[str] = ["=== Mental Models ==="]
        for mm in mental_models:
            title = mm.get("title", "(untitled)")
            kind = mm.get("kind", "general")
            content = (mm.get("content") or "").strip()
            mm_section.append(f"## {title} (kind={kind})")
            if content:
                mm_section.append(_truncate(content, 1500))
        if len(mm_section) > 1:
            parts.append("\n\n".join(mm_section))

    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Directive answerer prompt (Gap 1 + Gap 4)
# ---------------------------------------------------------------------------

# Ported from Hindsight ``_get_context_instructions`` (LME benchmark
# lines 244-340 + LoCoMo benchmark equivalent). Kept verbatim where
# the rules are bench-agnostic; the per-Q-type preface is added at
# the head so the answerer reads the right block first.
_DIRECTIVE_PROMPT_CORE = """\
**Understanding the Retrieved Context:**
The context contains memory facts extracted from previous conversations, paired with source chunks (raw conversation excerpts) and — when available — entity-observations and mental-model summaries.

1. **Fact**: A high-level summary / atomic statement extracted from a conversation.
2. **Source Chunks**: The actual raw conversation text. **This is your primary source for detailed information.** When facts seem ambiguous, prioritise what the chunks say verbatim.
3. **Entity Observations**: Consolidated cross-session claims with computed trends (new / strengthening / stable / weakening / stale) and proof counts. Use these for stable patterns about a user or entity.
4. **Mental Models**: Curated, user-authored or auto-compiled summaries (preference / directive / general). Treat ``directive`` mental models as **hard rules** the user has installed — apply them as preference overrides.
5. **Temporal Information**: Each fact may carry an ``occurred`` (when the event happened) or ``mentioned`` (when it was discussed) timestamp. Use these to understand timelines and resolve conflicts (prefer more recent info).

**Date Calculations (CRITICAL — read carefully):**
- When calculating days between two dates: count as (B - A). Jan 1 → Jan 8 = 7 days (not 8).
- "X days ago" from the Question Date means: Question Date minus X days.
- When a fact says "three weeks ago" on a particular mentioned date, that refers to 3 weeks before THAT mentioned date, NOT the question date.
- Always convert relative times ("last Friday", "two weeks ago") to absolute dates BEFORE comparing.
- Double-check arithmetic — off-by-one errors are very common.
- Read questions carefully for time anchors. "How many days ago did X happen when Y happened?" asks for the time between X and Y, NOT between X and the question date.

**Handling Relative Times in Facts:**
- If a fact says "last Friday" or "two weeks ago", anchor it to the fact's mentioned date, NOT the question date.
- First convert ALL relative references to absolute dates, then answer the question.
- Show your date-conversion work in your reasoning.

**Counting Questions (CRITICAL for "how many" questions):**
- **Scan ALL facts first** — go through every fact before counting; do not stop early.
- **List each item explicitly in your reasoning** before giving the count: "1. X, 2. Y, 3. Z = 3 total".
- **Check all facts AND chunks** before the final count.
- **Watch for duplicates**: the same item may appear in multiple facts; deduplicate if two facts describe the same underlying item / event.
- **Watch for different descriptions of the same thing**: "Dr. Patel (ENT specialist)" and "the ENT specialist" may be one doctor.
- **Don't over-interpret**: a project you "completed" is different from one you're "leading".
- **Don't double-count** when the same event is mentioned in two conversations.

**Disambiguation (CRITICAL — many errors come from over-counting):**
- **Assume overlap by default**: two facts describing similar events (same type, similar timeframe, similar details) are probably the SAME event unless clear evidence says otherwise.
- If a person has a name AND a role, check whether they're the same person before counting separately.
- If an amount is mentioned multiple times on different dates, check whether it's the same event.
- **Check for aliases**: "my college roommate's wedding" and "Emily's wedding" might be the same.
- **Check for time-period overlap**: two "week-long breaks" in overlapping periods are likely the same break.
- **When in doubt, undercount** — better to miss a duplicate than count the same thing twice.

**Question Interpretation:**
- "How many X before Y?" — count only X that happened BEFORE Y, not Y itself.
- "How many X in the last week / month?" — calculate the date range from the Question Date, then filter.
- Pay attention to qualifiers: "before", "after", "initially", "currently", "in total".

**When to Say "I Don't Know":**
- If the question asks about something not in the retrieved context, say so explicitly.
- If comparing two things and only one is mentioned, say which one is missing.
- Don't guess or infer dates not explicitly stated.
- **Partial knowledge is OK**: if asked about two things and you have info on one, provide it and note what's missing — do not just say "I don't know".

**For Recommendation / Preference Questions:**
- **DO NOT invent specific recommendations** (no made-up product names, course titles, paper titles, channel names).
- **DO mention specific brands / products / providers the user ALREADY uses** from the context — by name.
- Describe WHAT KIND of recommendation the user would prefer, referencing their existing tools / brands explicitly.
- First scan ALL facts for the user's existing tools, brands, stated preferences. Structure the answer around those, not around generic categories.
- If a ``directive`` mental model exists, treat it as a hard rule and structure the recommendation around it.

**How to Answer:**
1. Scan ALL facts to find relevant memories — do not stop after finding a few.
2. Read the source chunks carefully — they contain the actual details.
3. Convert all relative times to absolute dates.
4. Use temporal information to understand when things happened.
5. Synthesise across multiple facts when needed.
6. If facts conflict, prefer more recent information.
7. Double-check date calculations before answering.
8. For counting questions, list each unique item in reasoning, then count.
9. For recommendations, reference the user's existing tools / experiences / preferences explicitly.
"""


# M23 — per-Q-type FOCUSED prompts (replaces the M22 monster directive).
#
# M22's lesson: porting Hindsight's ~95-line LME directive prompt and
# applying it to every question across both benches REGRESSED LME by
# −4q overall and SSP specifically by −2q (the category the directive
# was supposed to win on). Same anti-composition failure shape as
# M18b's B5-stack — too many rules dilute the answerer's focus on the
# task at hand.
#
# Hindsight's actual strategy is the OPPOSITE: LME uses the long
# directive but LoCoMo uses a 10-line prompt routed through their
# reflect engine. Their per-bench prompts are tightly focused on what
# that bench actually tests.
#
# M23 generalises that insight: each question category gets its OWN
# small focused prompt with only the rules that apply to that shape.
# No giant directive. No category blocks inside one mega-prompt. Just
# "here's a SSP question — apply these 5 SSP-specific rules."
#
# Each focused prompt is 5-15 lines, self-contained, and references
# patterns the answerer can actually find in our retrieval shape
# (facts + source chunks + entity observations + mental models).

_QTYPE_FOCUSED_PROMPTS: dict[str, str] = {
    # ── LME — single-session-preference ───────────────────────────────
    # M19a peaked SSP at 5/5 in run-2 with the enriched fact extraction
    # (Hindsight `why` parity). The win came from preserving the user's
    # original framing in the fact's text. The answerer's job here is
    # to extract that framing back out.
    "single-session-preference": """\
**Question shape: the user stated a preference. Find it and quote it.**

1. Scan the facts for the user's preference statement — look for "I prefer", \
"I love", "I usually", "I always", "my favourite", or any superlative the \
user used about themselves.
2. The fact's text was extracted to preserve the user's original framing \
(brand names, conditions, reasons). Quote that framing back rather than \
paraphrasing into generic terms.
3. If recommendation is requested: do NOT invent new products/courses/titles. \
Reference brands/products the user ALREADY uses, by name.
4. If a `kind=directive` mental model exists, treat it as a hard rule and \
structure the answer around it.
5. Read the source chunks for the verbatim phrasing — they preserve the \
implicit cues that extracted facts may miss.
""",

    # ── LME — single-session-user ─────────────────────────────────────
    "single-session-user": """\
**Question shape: a specific fact about the user from ONE session. Be literal.**

1. Find ONE fact in the source chunks that directly answers the question.
2. Quote the chunk's exact wording. Avoid synthesis across sessions.
3. For counting / quantity questions, list the items explicitly before \
giving the number. Watch for duplicates (same item referenced multiple \
times = ONE item).
4. If the answer isn't directly in any chunk, say so. Do NOT infer.
""",

    # ── LME — single-session-assistant ────────────────────────────────
    "single-session-assistant": """\
**Question shape: what did the assistant say or recommend in ONE session.**

1. Find the assistant's statement in the source chunks.
2. Quote the assistant's exact phrasing — the question wants to hear back \
what the assistant said, not a paraphrase.
3. If multiple assistant statements are relevant, pick the most specific \
or most recent.
""",

    # ── LME — multi-session ───────────────────────────────────────────
    "multi-session": """\
**Question shape: synthesise evidence from MULTIPLE sessions.**

1. Find evidence from at least 2 distinct sessions / facts before answering.
2. List the supporting facts inline (1, 2, 3...) so the synthesis is \
traceable. Each numbered fact should come from a different session.
3. Look at the entity observations / mental models sections first — they \
already consolidate cross-session evidence and often give the answer \
without needing to re-synthesise from raw facts.
4. If facts conflict: prefer the more recent or more specific one and \
note the conflict explicitly.
""",

    # ── LME — knowledge-update ────────────────────────────────────────
    "knowledge-update": """\
**Question shape: the user's CURRENT state (latest value of a changing field).**

1. Find ALL facts about the field in question (job, location, status, \
relationship, etc.).
2. Use the MOST RECENT fact. Earlier facts about the same field are \
HISTORICAL, not current.
3. Mention the prior value only if the question explicitly asks about \
change history. Otherwise stick to the current value.
4. Use the fact timestamps (`occurred` or `mentioned`) to determine \
recency — not the order facts appear in the context.
""",

    # ── LME — temporal-reasoning / LoCoMo — temporal ──────────────────
    "temporal-reasoning": """\
**Question shape: date arithmetic. Convert relative → absolute first.**

1. Find the ORIGINAL mention date for each event. Older facts are often \
the right ones — do not over-index on the most recent.
2. **PREFER PRE-COMPUTED HINTS.** When a fact's `When:` or `Event date:` \
line includes a parenthetical hint like "(4 weeks ago)" or "(28 days \
ago)", USE that value DIRECTLY — it's already computed against the \
question date. Do NOT re-derive your own; the hint is more reliable \
than mental arithmetic.
3. Convert any relative phrase ("last week", "3 days ago", "a few months \
back") to an absolute date BEFORE comparing. Anchor relative phrases to \
the fact's `mentioned` date, NOT the question date.
4. For date-difference questions where no hint exists, show the \
arithmetic explicitly. Count days as (B - A); off-by-one errors are \
common. Show your work.
5. **NEVER fabricate specific durations** ("X years", "N months", \
"6 weeks ago") if the retrieved facts don't contain explicit dates \
supporting the calculation. Saying "I don't have enough information \
to calculate this" is the CORRECT answer when an endpoint date is \
missing from the retrieved facts. Do NOT guess.
6. If you don't have both endpoints, say it's not possible to calculate \
and explain WHICH endpoint is missing (helps the user understand the \
abstention).
""",
    "temporal": """\
**Question shape: date arithmetic. Convert relative → absolute first.**

1. Find the ORIGINAL mention date for each event. Older facts are often \
the right ones — do not over-index on the most recent.
2. Convert any relative phrase ("last week", "3 days ago", "a few months \
back") to an absolute date BEFORE comparing. Anchor relative phrases to \
the fact's `mentioned` date, NOT the question date.
3. For date-difference questions, show the arithmetic explicitly. Count \
days as (B - A); off-by-one errors are common.
4. If you don't have both endpoints, say it's not possible to calculate \
and explain why.
""",

    # ── LoCoMo — multi-hop ────────────────────────────────────────────
    # Multi-hop was M22's only category WIN (+2q with the directive
    # prompt). The disambiguation discipline is what helped. Keep that
    # discipline in the focused prompt.
    "multi-hop": """\
**Question shape: combine evidence from MULTIPLE facts to derive the answer.**

1. Find the bridge memories — facts from different sessions that, when \
combined, produce the answer.
2. List the supporting facts inline (1, 2, 3...) so the synthesis is \
traceable.
3. **Disambiguate aggressively**: if two facts describe similar events \
(same type, similar timeframe, similar details), assume they're the SAME \
event unless clear evidence says otherwise. Same person with name + role \
= one person. Aliases like "Emily's wedding" and "my roommate's wedding" \
likely refer to the same event.
4. When in doubt about whether two mentions are the same item, undercount \
rather than double-count.
""",

    # ── LoCoMo — open-domain ──────────────────────────────────────────
    # M22 regressed open-domain by −5pp under the directive prompt
    # (too many rules distracted from broad synthesis). Focused prompt
    # gives ONE simple instruction: be detailed, reference specifics.
    "open-domain": """\
**Question shape: open-ended. Be detailed and reference specifics by name.**

1. Include rich detail from multiple relevant sources.
2. Reference specific entities, dates, brand names, and values BY NAME \
rather than paraphrasing into generic terms.
3. If the question asks for a location, include the location name explicitly.
4. Don't truncate — the answerer wants comprehensive context.
""",

    # ── LoCoMo — single-hop ───────────────────────────────────────────
    "single-hop": """\
**Question shape: answer comes from ONE fact. Find it and quote.**

1. Find the single fact in the source chunks that answers the question.
2. Answer literally; don't over-synthesise.
3. If the fact isn't there, say so.
""",
}


# Shared minimal prelude — applies to every question regardless of type.
# Contains ONLY universals: how to read the context format. No
# question-specific rules. The category-focused prompt handles those.
_SHARED_PRELUDE = """\
**Context format:**
- `Fact N:` — atomic facts extracted from past conversations. Each fact \
includes a `Source chunk:` block underneath with the raw conversation \
excerpt the fact was extracted from. Use the source chunk for verbatim \
wording, implicit cues, and details the extracted fact may have lost.
- `=== Additional Source Chunks (unpaired) ===` (when present) — sections / \
wikis that weren't directly paired with any fact above. Same kind of raw \
text, just unanchored.
- `=== Entity Observations ===` (when present) — cross-session consolidations \
with a computed trend (new / strengthening / stable / weakening / stale) and \
proof count. Treat as authoritative for stable patterns.
- `=== Mental Models ===` (when present) — curated summaries. `kind=directive` \
rows are USER-AUTHORED hard rules; apply as preference overrides.

**Timestamp interpretation:**
When a fact's `When:` line shows both `occurred:` and `mentioned:`, \
`occurred:` is when the event happened and `mentioned:` is when the user \
told us about it. Anchor relative phrases ("last week", "3 days ago") to \
`mentioned:`, NOT to the question date.

**Event date (M31 Fix 4 — prefer when present):**
When a fact carries an `Event date: YYYY-MM-DD` line, that value has \
already been resolved deterministically at retain time from any \
relative phrase in the fact's text (e.g. "last Tuesday" → "2024-05-07"). \
**Use that date verbatim** for any date arithmetic — do NOT re-resolve \
relative phrases in the fact's text against the question date. The \
`Event date:` is the source of truth for the fact's most-prominent date.

**Confidence handling (M31, operational):**
Each fact may carry a `Confidence: 0.NN` line. Use it to calibrate your \
language and avoid confident-wrong answers:
- **Confidence ≥ 0.85 (HIGH)** — quote the fact directly. Example: "Your \
favourite coffee is the Ethiopian pour-over at Blue Bottle."
- **Confidence 0.5–0.85 (MEDIUM)** — soften to natural attribution. Example: \
"You've mentioned the Ethiopian pour-over as a favourite." Avoid superlatives \
("definitely", "always") unless multiple supporting facts agree.
- **Confidence < 0.5 (LOW)** — explicitly flag uncertainty. Example: "I'm \
not highly confident on this, but the closest match suggests you preferred \
the Ethiopian pour-over." Do NOT pretend higher confidence than the data \
supports.
- **When multiple high-confidence facts conflict** (same field, different \
values), surface both with their `mentioned:` dates and let the user \
disambiguate. Example: "On 2024-03-01 you told me X; on 2024-06-15 you \
told me Y."

When a question asks for verbatim wording or a specific detail, prefer the \
Source chunk under the matching fact over the fact's summary text. The fact \
is the searchable summary; the source chunk is the ground truth.

If retrieved evidence is insufficient, say so — partial knowledge ("I have X \
but not Y") is better than fabrication. Do NOT default to "I don't have \
enough information" when ANY relevant fact is present; instead state what \
you DO have and flag what's missing.
"""


# M44 — Cross-cutting addenda (counting / question interpretation /
# IDK precision). Gated by ASTROCYTE_M44_PROMPT_FIXES so we can A/B vs
# v015s2 without perturbing the shipped baseline. When ON, these blocks
# get concatenated after _SHARED_PRELUDE so they apply to every qtype.
#
# Audit source: Hindsight LME bench prompt
# (hindsight-dev/benchmarks/longmemeval/longmemeval_benchmark.py
# _get_context_instructions). We already ported the date-arithmetic +
# recommendation discipline (M19a, M31d, M39) and have our own confidence
# / event_date blocks. These addenda close the remaining gaps:
#
# - Counting discipline (Fix A): scan ALL, list explicitly, dedup, undercount
# - Question interpretation (Fix C): "X before Y", "in last week/month"
# - IDK precision (Fix D): comparison-with-missing-side, partial knowledge OK
#
# Fix B (multi-session disambiguation) is in the MS qtype prompt above —
# it's qtype-specific so it lives with the focused prompt, not the
# cross-cutting addendum.

_M44_COUNTING_AND_COMPARISON = """\

**Counting & comparison discipline (CRITICAL for "how many" questions):**
- **Scan ALL facts first** — go through every retrieved fact before counting; \
don't stop after finding a few.
- **List each unique item in your reasoning** before giving the count: \
"1. X, 2. Y, 3. Z = 3 total". Never just emit a number.
- **Watch for duplicates** — the same item may appear in multiple facts under \
different wording. Examples: "Dr. Patel (ENT specialist)" and "the ENT \
specialist" are likely the same person; two "week-long breaks" in overlapping \
time periods are likely the same break; "my college roommate's wedding" and \
"Emily's wedding" are likely the same event.
- **Same name + role = one item.** Same event-type + same timeframe + \
overlapping details = one item.
- **When in doubt, undercount.** It's better to miss a near-duplicate than \
to count the same thing twice.
"""

_M44_QUESTION_INTERPRETATION = """\

**Question interpretation (read carefully):**
- "How many X **before** Y?" → count only X that happened BEFORE Y. Do NOT \
include Y itself in the count.
- "How many properties viewed before making an offer on Z?" → count OTHER \
properties, not Z.
- "X in the last week / month / N days?" → first compute the exact date \
range from the question date (Question Date − N days), then filter facts \
to that range.
- Pay close attention to qualifiers: "before", "after", "initially", \
"currently", "in total", "ever". They change which subset you count.
"""

_M44_IDK_PRECISION = """\

**When evidence is partial — be precise about what's missing:**
- If comparing X and Y (e.g. "which happened first?" / "which is bigger?" / \
"which did you prefer?") but only one is in the retrieved context, \
**explicitly state the other is missing** — do not guess to fill the gap.
- **Partial knowledge is OK.** If asked about two things and you have info on \
only one, provide what you know and explicitly note what's missing. Do NOT \
default to "I don't have enough information" when ANY relevant fact is \
present.
- If you cannot find a specific piece of information after checking all \
facts and chunks, admit it — but say WHICH piece is missing.
"""


def _m44_addenda_enabled() -> bool:
    """Gate for the M44 cross-cutting prompt addenda.

    Default ON as of 2026-05-23 — v015w LME bench validated M44 at the
    new mt_8192 ship-floor (74.4% vs 71.1% prior). The mt_1024 cutoff
    regressed −6q (real signal ≈ −3 to −4q after stripping judge noise);
    surgical fixes are queued for v015w-fix. Set to "0" to opt out and
    revert to the v015s2 baseline behavior. See
    ``docs/_design/v0.15.0-ship-decision.md`` Appendices A + B.
    """
    import os as _os  # noqa: PLC0415

    return _os.environ.get("ASTROCYTE_M44_PROMPT_FIXES", "1").lower() in (
        "1", "true", "yes",
    )


# M44 Fix B — strengthened multi-session prompt (disambiguation +
# comparison precision). Layered as an OVERRIDE in _qtype_focused_prompt
# rather than baked into _QTYPE_FOCUSED_PROMPTS so the same gate
# (ASTROCYTE_M44_PROMPT_FIXES) controls all M44 deltas — keeps the A/B
# clean against the v015s2 baseline.
_M44_MULTI_SESSION_PROMPT = """\
**Question shape: synthesise evidence from MULTIPLE sessions.**

1. Find evidence from at least 2 distinct sessions / facts before answering.
2. List the supporting facts inline (1, 2, 3...) so the synthesis is \
traceable. Each numbered fact should come from a different session.
3. Look at the entity observations / mental models sections first — they \
already consolidate cross-session evidence and often give the answer \
without needing to re-synthesise from raw facts.
4. **Disambiguate aggressively**: if two facts describe similar events \
(same type, similar timeframe, similar details), assume they're the SAME \
event unless clear evidence says otherwise. Same person with name + role \
= one person. Aliases like "Emily's wedding" and "my roommate's wedding" \
likely refer to the same event. When in doubt about whether two mentions \
are the same item, **undercount** rather than double-count.
5. If facts conflict: prefer the more recent or more specific one and \
note the conflict explicitly.
6. **Comparison precision**: if comparing X and Y (e.g. "which happened \
first?") but only one is in the retrieved context, explicitly state the \
other is missing — do NOT guess to fill the gap.
"""


def _qtype_focused_prompt(question_type: str | None) -> str:
    """Return the focused prompt for a question type, or a generic fallback.

    When ``ASTROCYTE_M44_PROMPT_FIXES`` is enabled, the multi-session
    prompt is overridden with the M44-strengthened variant
    (:data:`_M44_MULTI_SESSION_PROMPT`). All other qtypes are unchanged.
    """
    if not question_type:
        return ""
    key = str(question_type).lower().strip()
    if key == "multi-session" and _m44_addenda_enabled():
        return _M44_MULTI_SESSION_PROMPT
    return _QTYPE_FOCUSED_PROMPTS.get(key, "")


def build_hindsight_prompt(
    question: str,
    *,
    search_results: list[dict[str, Any]],
    reference_date: str | None = None,
    user_profile: dict | None = None,
    observations: list[dict[str, Any]] | None = None,
    mental_models: list[dict[str, Any]] | None = None,
    question_type: str | None = None,
) -> str:
    """Compose a per-Q-type focused prompt + structured context (M23).

    Drop-in replacement for ``get_answer_generation_prompt`` in the
    Mem0 LoCoMo / LME runners. Signature is a superset (extra kwargs
    are optional) so the monkey-patched call site stays compatible.

    M23 design: the prompt is small and focused on the question's
    actual shape. No monster directive. The shared prelude explains
    the context format (1 paragraph); the category-focused prompt
    gives the 4-5 rules that matter for this question type; the
    universal answer guidelines stay minimal. Total prompt size
    drops from ~95 lines (M22) to ~25-35 lines (M23) — letting the
    answerer focus on the question rather than parsing rules.
    """
    context = format_context_structured(
        search_results,
        observations=observations,
        mental_models=mental_models,
        reference_date=reference_date,  # M31d Fix D — for time-delta hints
    )

    profile_section = ""
    if user_profile:
        try:
            from benchmarks.locomo.prompts import _format_user_profile  # noqa: PLC0415

            profile_section = _format_user_profile(user_profile)
            if profile_section:
                profile_section = profile_section + "\n\n"
        except (ImportError, AttributeError):
            pass

    formatted_qdate = reference_date or "Not specified"
    focused = _qtype_focused_prompt(question_type)
    if not focused:
        # Generic fallback when question_type is missing or unknown.
        focused = (
            "**Answer the question using the retrieved context. Be specific; "
            "reference entities, dates, and values by name; quote source "
            "chunks when wording matters.**\n"
        )

    # M44 — cross-cutting prompt addenda (counting / question
    # interpretation / IDK precision). Gated; concatenated AFTER the
    # shared prelude so they apply uniformly across qtypes without
    # changing the focused prompts' wording. When OFF, the prompt is
    # byte-identical to the v015s2 baseline.
    #
    # The v015w-fix variant attempted to qtype-route the counting block
    # and soften the IDK block, but it regressed mt_8192 −9q (erased the
    # v015w win) and didn't recover mt_1024. The addenda are tightly
    # coupled to the LLM's calibrated behavior — partial application
    # broke the calibration. v015w (this code path) is the shipping
    # config. See ``docs/_design/v0.15.0-ship-decision.md`` Appendix B.
    m44_block = ""
    if _m44_addenda_enabled():
        m44_block = (
            _M44_COUNTING_AND_COMPARISON
            + _M44_QUESTION_INTERPRETATION
            + _M44_IDK_PRECISION
        )

    return f"""You are answering a user's question from retrieved memory context.

{_SHARED_PRELUDE}{m44_block}
{focused}
{profile_section}Question: {question}
Question Date: {formatted_qdate}

Retrieved Context:
{context}


Answer:
"""


# ---------------------------------------------------------------------------
# Monkey-patch installer
# ---------------------------------------------------------------------------


def maybe_install_hindsight_answerer_patch(bench_name: str) -> bool:
    """Install the Hindsight answerer + per-Q-type prompt patch.

    When ``ASTROCYTE_M22_HINDSIGHT_ANSWERER=1``, monkey-patches:

    - ``benchmarks.{bench_name}.prompts.get_answer_generation_prompt``
      → drop-in replacement that emits the Hindsight-style structured
      context + directive prompt. The upstream ``process_question``
      continues to call ``get_answer_generation_prompt(question,
      sliced, reference_date=..., user_profile=...)`` with no
      changes — we just swap what comes out.

    Per-Q-type routing requires the question_type to be in scope. The
    upstream call site doesn't pass it directly, so we route via a
    ``contextvar`` populated by a second monkey-patch on
    ``process_question`` that sets the var per-question before
    calling into the patched prompt. See
    :func:`_install_process_question_qtype_shim` below.

    Returns True when patches landed; False when the flag was off or
    the upstream module couldn't be located (idempotent).
    """
    if not is_enabled():
        return False

    mod_name = f"benchmarks.{bench_name}.prompts"
    if mod_name not in sys.modules:
        try:
            __import__(mod_name)
        except ImportError as exc:
            print(
                f"[hindsight-answerer] could not import {mod_name}: {exc}",
                file=sys.stderr,
            )
            return False

    mod = sys.modules[mod_name]
    if getattr(mod, "_ASTROCYTE_M22_HINDSIGHT_INSTALLED", False):
        return True  # idempotent

    original = getattr(mod, "get_answer_generation_prompt", None)
    if original is None:
        print(
            f"[hindsight-answerer] {mod_name} has no get_answer_generation_prompt — skip",
            file=sys.stderr,
        )
        return False

    def patched(
        question: str,
        search_results: list,
        reference_date: str | None = None,
        user_profile: dict | None = None,
    ) -> str:
        qtype = _CURRENT_QTYPE.get()
        observations = _CURRENT_OBSERVATIONS.get()
        mental_models = _CURRENT_MENTAL_MODELS.get()
        return build_hindsight_prompt(
            question,
            search_results=search_results,
            reference_date=reference_date,
            user_profile=user_profile,
            observations=observations,
            mental_models=mental_models,
            question_type=qtype,
        )

    mod.get_answer_generation_prompt = patched
    mod._ASTROCYTE_M22_HINDSIGHT_INSTALLED = True

    _install_process_question_qtype_shim(bench_name)

    print(
        f"[hindsight-answerer] installed M22 Hindsight answerer prompt + structured context for {bench_name}",
        file=sys.stderr,
    )
    return True


# ---------------------------------------------------------------------------
# Per-question contextvars (carry qtype + obs + mm into the patched prompt)
# ---------------------------------------------------------------------------

import contextvars  # noqa: E402

_CURRENT_QTYPE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "astrocyte_m22_qtype", default=None,
)
_CURRENT_OBSERVATIONS: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar(
    "astrocyte_m22_observations", default=None,
)
_CURRENT_MENTAL_MODELS: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar(
    "astrocyte_m22_mental_models", default=None,
)
# M31 Fix 2 — session_id contextvar. Populated by the per-question
# wrapper from LME question metadata (``question_session_id``); read
# by ``astrocyte_client.search()`` via ``current_session_id()`` and
# passed to ``fact_recall(session_filter=...)``. The plumbing is the
# same mechanism as the qtype contextvar to avoid widening the
# upstream ``mem0.search(...)`` SPI surface.
_CURRENT_SESSION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "astrocyte_m31_session_id", default=None,
)


def current_session_id() -> str | None:
    """Public accessor for the per-question session_id contextvar.

    ``astrocyte_client.search()`` calls this to obtain the session_id
    set by the per-question wrapper. Returns ``None`` outside the
    wrapper's context (the back-compat shape — no filter applied).
    """
    return _CURRENT_SESSION_ID.get()


def _install_process_question_qtype_shim(bench_name: str) -> None:
    """Wrap upstream ``process_question`` so we can set the qtype + obs + mm
    contextvars before the patched ``get_answer_generation_prompt`` fires.

    The LME runner has two flavours (``process_question_answerer`` and
    ``process_question_retrieval``); the LoCoMo runner has one
    (``process_question``). We wrap whichever exists.
    """
    run_mod_name = f"benchmarks.{bench_name}.run"
    if run_mod_name not in sys.modules:
        try:
            __import__(run_mod_name)
        except ImportError:
            return
    run_mod = sys.modules[run_mod_name]

    targets: list[str] = []
    if hasattr(run_mod, "process_question"):
        targets.append("process_question")
    if hasattr(run_mod, "process_question_answerer"):
        targets.append("process_question_answerer")
    if hasattr(run_mod, "process_question_retrieval"):
        targets.append("process_question_retrieval")

    for name in targets:
        original = getattr(run_mod, name)
        if getattr(original, "_astrocyte_m22_wrapped", False):
            continue

        # Signature-agnostic wrapper. The bench runners have different
        # signatures:
        #   - LoCoMo: process_question(qa, qa_idx, conv_idx, user_id, mem0, ...)
        #   - LME:    process_question_answerer(question=..., user_id=..., mem0=..., ...)
        # Both pass mostly via kwargs (the LME runner always uses kwargs,
        # the LoCoMo runner has been observed to mix positional + kwargs).
        # We accept any signature and find the question dict + mem0 +
        # user_id by inspecting both call conventions.
        async def _wrapper(  # noqa: ANN001 — generic wrapper
            *args: Any,
            _orig=original,
            _name=name,
            **kwargs: Any,
        ) -> dict:
            # Locate the question dict — different runners use different
            # parameter names but the value is always a dict containing
            # the question fields.
            qa: dict | None = None
            for key in ("qa", "question"):
                if key in kwargs and isinstance(kwargs[key], dict):
                    qa = kwargs[key]
                    break
            if qa is None and args:
                # First positional arg by convention is the question dict.
                if isinstance(args[0], dict):
                    qa = args[0]

            # Locate mem0 + user_id (always passed by name in LME; can be
            # positional in LoCoMo).
            mem0 = kwargs.get("mem0")
            user_id = kwargs.get("user_id")
            if mem0 is None or user_id is None:
                # LoCoMo positional convention: (qa, qa_idx, conv_idx, user_id, mem0, ...)
                if len(args) >= 5:
                    if user_id is None and isinstance(args[3], str):
                        user_id = args[3]
                    if mem0 is None:
                        mem0 = args[4]

            # Extract question type.
            qtype: str | None = None
            if qa is not None:
                qtype = qa.get("question_type") or qa.get("type")
                if qtype is None and "category" in qa:
                    category_names = getattr(run_mod, "CATEGORY_NAMES", {})
                    qtype = category_names.get(qa["category"])

            # M31 Fix 2 — extract session_id from question metadata when
            # the dataset provides one (LME questions typically include
            # ``question_session_id``; LoCoMo questions usually don't).
            # When present, ``astrocyte_client.search()`` reads it via
            # the ``_CURRENT_SESSION_ID`` contextvar and forwards it to
            # ``fact_recall(session_filter=...)``. ``None`` (the
            # default) preserves v0.14.0 behaviour (no session scoping).
            session_id: str | None = None
            if qa is not None:
                session_id = (
                    qa.get("question_session_id")
                    or qa.get("session_id")
                    or (qa.get("metadata", {}) or {}).get("session_id")
                )

            # Fetch observations + mental models if client supports it.
            # M30-L2 — these two SPI hits are independent (different tables,
            # no shared state); run concurrently via asyncio.gather so we
            # pay one round-trip instead of two. Saves ~0.5-1s per question
            # on the bench pool. ``return_exceptions=True`` keeps per-branch
            # failure isolation — an obs SPI hiccup must not poison mm.
            observations: list[dict] = []
            mental_models: list[dict] = []
            if mem0 is not None and user_id is not None:
                import asyncio as _asyncio  # noqa: PLC0415

                fetch_obs = getattr(mem0, "list_observations_for_bench", None)
                fetch_mm = getattr(mem0, "list_mental_models_for_bench", None)
                obs_coro = fetch_obs(user_id, limit=20) if fetch_obs is not None else None
                mm_coro = fetch_mm(user_id, limit=10) if fetch_mm is not None else None
                if obs_coro is not None or mm_coro is not None:
                    # Build a sparse argv → run only the present coroutines.
                    pending: list[Any] = []
                    slots: list[str] = []
                    if obs_coro is not None:
                        pending.append(obs_coro)
                        slots.append("obs")
                    if mm_coro is not None:
                        pending.append(mm_coro)
                        slots.append("mm")
                    gathered = await _asyncio.gather(*pending, return_exceptions=True)
                    for slot, result in zip(slots, gathered, strict=True):
                        if isinstance(result, BaseException):
                            continue  # leave the default empty list
                        if slot == "obs":
                            observations = result  # type: ignore[assignment]
                        else:
                            mental_models = result  # type: ignore[assignment]

            tok_q = _CURRENT_QTYPE.set(qtype)
            tok_o = _CURRENT_OBSERVATIONS.set(observations or None)
            tok_m = _CURRENT_MENTAL_MODELS.set(mental_models or None)
            tok_s = _CURRENT_SESSION_ID.set(session_id)  # M31 Fix 2
            try:
                return await _orig(*args, **kwargs)
            finally:
                _CURRENT_QTYPE.reset(tok_q)
                _CURRENT_OBSERVATIONS.reset(tok_o)
                _CURRENT_MENTAL_MODELS.reset(tok_m)
                _CURRENT_SESSION_ID.reset(tok_s)  # M31 Fix 2

        _wrapper._astrocyte_m22_wrapped = True  # type: ignore[attr-defined]
        setattr(run_mod, name, _wrapper)

    print(
        f"[hindsight-answerer] wrapped {targets} in {run_mod_name} for qtype + obs + mm contextvar plumbing",
        file=sys.stderr,
    )
