"""M11.2 — mental-model compile pass.

Synthesizes stable user preferences / persona / habits / background
from a document's sections and persists them as ``MentalModel`` rows
in the bank's :class:`~astrocyte.provider.MentalModelStore`. Direct
port of Hindsight's mental-model tier (saved-reflect summaries
recalled BEFORE observations and raw memories).

Different from :mod:`section_compile` (wiki pages):

- **Wiki pages** are topic-clustered observations
  ("User visited 3 doctors", "User worked on 5 model kits").
  Generated per cluster via DBSCAN. Carry section-grain provenance.
- **Mental models** are DURABLE user-profile statements
  ("User prefers Sony cameras", "User practices Spanish 3×/week").
  Generated per document via one LLM call across all sections.
  Span the whole document — no per-cluster scope.

Why both layers: a question like "recommend a hotel in Miami" wants
the user-profile fact ("user prefers ocean views") regardless of
which topic-cluster that was discussed in. Wiki pages cluster by
topic; mental models cluster by user-profile dimension.

Generic across LME / LoCoMo / future benches — the prompt asks for
PROFILE facts, not bench-specific shapes.

See:
- ``docs/_design/recall.md`` §14 (M11 plan)
- ``hindsight-api-slim/hindsight_api/engine/reflect/observations.py``
  for Hindsight's analogous structure (their ``Observation`` rows on
  ``MentalModel``).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from astrocyte.types import MentalModel, Message

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider, MentalModelStore, PageIndexStore

_logger = logging.getLogger("astrocyte.pipeline.mental_model_compile")


_COMPILE_PROMPT = """\
You are extracting STABLE user profile facts from a conversation \
transcript. The user is the speaker labeled "user" in chat turns.

Output a JSON object with one key, ``models``, containing an array \
of mental-model objects. Each model has:
- "title": 3-7 word noun phrase naming the profile dimension \
(e.g. "Photography gear preference", "Diet", "Dance interests")
- "content": one declarative sentence stating the user's stable \
preference, habit, persona, or background. Use specific entities \
when the user named them. The reader will use this as authoritative \
context for "recommend X" / "what do I like" / "tell me about my X" \
questions.

What counts as a mental model (extract these):
- Stable preferences: "User prefers Sony cameras over Canon."
- Persona / background: "User is a parent of two children, lives in \
Brooklyn, works in tech."
- Recurring habits: "User attends weekly hip-hop dance classes at \
Street Beats."
- Stable opinions: "User finds yoga more relaxing than running."
- Hobbies / projects: "User is building a home maintenance app."
- Skills / expertise: "User has 10 years of photography experience."
- Values / goals: "User is saving for a Europe trip in 2024."

What does NOT count (ignore these):
- One-off events ("Yesterday I went to the doctor")
- Casual mentions without preference signal ("I had coffee")
- Things the assistant said about the user (only user's own claims)
- Speculation or one-time questions

Rules:
- Each model must reflect something the user EXPLICITLY stated or \
strongly implied across multiple sections (not single-mention)
- Be specific about entities when known (brand names, places, etc.)
- Cap at 12 models total — prefer the most-discussed dimensions
- If sections are generic chitchat with no stable profile signals, \
return ``{{"models": []}}``

Examples of good output:
{{"models": [
  {{"title": "Photography gear preference", "content": "User shoots \
with Sony A7 III and prefers Sony-compatible accessories like the 24-70mm \
G Master lens."}},
  {{"title": "Movie genre taste", "content": "User strongly prefers \
stand-up comedy specials on Netflix, especially recent ones (Ali Wong, \
John Mulaney)."}},
  {{"title": "Dance practice", "content": "User attends weekly hip-hop \
classes at Street Beats and enjoys contemporary as a secondary style."}},
  {{"title": "Diet", "content": "User follows a mostly-vegetarian \
diet with occasional fish; avoids dairy."}}
]}}

OUTPUT MUST BE VALID JSON. No prose around it.

Section summaries (chronological):
{sections}
"""


def _slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s[:60] or "model"


def _format_sections_for_prompt(sections, max_sections: int = 60) -> str:
    """Render section summaries chronologically for the compile prompt.

    Cap at ``max_sections`` to keep the prompt under ~6K tokens. We
    rank by line_num ascending (chronological order through the
    document) and take the first ``max_sections``; LME chat-history
    documents commonly have 30-50 sections so this rarely trims.
    """
    chronological = sorted(sections, key=lambda s: s.line_num)
    rendered = []
    for s in chronological[:max_sections]:
        summary = (s.summary or s.title or "").strip()
        if not summary:
            continue
        date = (
            s.session_date.strftime("%Y-%m-%d")
            if getattr(s, "session_date", None) is not None
            else "no-date"
        )
        rendered.append(f"[line={s.line_num} date={date}] {summary}")
    return "\n".join(rendered)


async def compile_mental_models_for_document(
    *,
    page_index_store: PageIndexStore,
    mental_model_store: MentalModelStore,
    bank_id: str,
    document_id: str,
    provider: LLMProvider,
    model: str | None = None,
) -> list[str]:
    """Extract mental models for one document and persist via
    :class:`MentalModelStore`.

    Returns the list of newly-upserted ``model_id``s. Idempotent: when
    models with the same ``model_id`` already exist for this bank, they
    are bumped to a new revision (the store's standard upsert
    semantics) rather than duplicated.

    Scoping: ``scope = f"document:{document_id}"`` — mirrors the wiki
    tier's pattern so the bench's per-question retrieval can filter
    cleanly to the right document without cross-contamination across
    sibling LME conversations in the same bank.
    """
    sections = await page_index_store.load_skeleton(document_id)
    if not sections:
        return []

    prompt = _COMPILE_PROMPT.format(
        sections=_format_sections_for_prompt(sections),
    )
    try:
        completion = await provider.complete(
            [Message(role="user", content=prompt)],
            model=model,
            max_tokens=900,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "mental_model_compile: LLM call failed doc=%s: %s",
            document_id, exc,
        )
        return []
    try:
        data = json.loads(completion.text)
    except json.JSONDecodeError as exc:
        _logger.warning(
            "mental_model_compile: JSON parse failed doc=%s: %s text=%r",
            document_id, exc, completion.text[:200],
        )
        return []
    raw_models = data.get("models") or []
    if not isinstance(raw_models, list):
        return []

    now = datetime.now(tz=timezone.utc)
    scope = f"document:{document_id}"
    upserted: list[str] = []
    for entry in raw_models:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title", "")).strip()
        content = str(entry.get("content", "")).strip()
        if not title or not content:
            continue
        model_id = f"mm:{document_id[:8]}:{_slugify(title)}"
        mm = MentalModel(
            model_id=model_id,
            bank_id=bank_id,
            title=title,
            content=content,
            scope=scope,
            source_ids=[f"{document_id}:doc"],
            revision=1,  # upsert assigns the real revision number
            refreshed_at=now,
        )
        try:
            await mental_model_store.upsert(mm, bank_id)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "mental_model_compile.upsert failed model_id=%s: %s",
                model_id, exc,
            )
            continue
        upserted.append(model_id)

    _logger.info(
        "mental_model_compile: doc=%s upserted %d models",
        document_id, len(upserted),
    )
    return upserted
