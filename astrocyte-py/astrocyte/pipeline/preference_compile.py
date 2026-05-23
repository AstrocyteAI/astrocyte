"""M14.6: consolidated user-preference compile.

Distils raw preference-type facts into structured consolidated
preferences, stored as :class:`MentalModel` rows with
``kind="preference"``. Matches Hindsight's mental-models pattern for
consolidated knowledge (their ``mental_models.subtype`` column).

Why a separate compile pass rather than enriching raw extraction:
- Hindsight tried the dedicated ``opinion`` fact_type and **removed
  it** (migration g2h3i4j5k6l7 — April 2026). Their evidence: schema
  rigidity at ingest didn't pan out; preferences are better handled
  at the compiled-knowledge layer downstream of fact extraction.
- Raw preference facts capture individual statements but lose the
  conditional context (when/why/under-what-conditions) the LME
  single-session-preference category probes. A consolidation pass
  with an LLM-judged synthesis can preserve that context.

Algorithm (one LLM call per document):

1. Pull all PageIndexFacts with ``fact_type='preference'`` for the doc.
2. If fewer than 2 preference facts, skip (no consolidation signal).
3. Send to LLM with a prompt asking for up to N consolidated
   preferences, each with title + content (the content preserves
   qualifier / condition / sentiment inline) + the source fact ids.
4. Save each as a :class:`MentalModel` with ``kind='preference'``,
   ``scope=f'document:{document_id}'``. Idempotent — skip if the
   document already has preference models (M14.6 v1; M14.7 may add
   incremental refresh).

The bench's existing :meth:`AstrocyteClient.get_user_profile` lists
mental models by ``scope`` and serialises them into the answerer's
``## User Profile`` block — preference-kind models surface there
automatically. No retrieval-path change needed for v1.

Cost: ~1 LLM call per document (gpt-4o-mini), ~$0.01-0.02. Marginal.

See:
- ``docs/_design/m13-m14-roadmap.md`` §4 (M14.6 placement)
- ``astrocyte.pipeline.mental_model_compile`` — sibling that produces
  ``kind='general'`` profile statements
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from astrocyte.types import MentalModel, Message

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider, MentalModelStore
    from astrocyte.types import PageIndexFact

_logger = logging.getLogger("astrocyte.pipeline.preference_compile")


_COMPILE_PROMPT = """\
You are consolidating a user's STATED PREFERENCES from a conversation \
transcript. You will be given the raw preference statements extracted \
from the conversation. Your job: produce up to {max_prefs} consolidated \
preferences that capture WHAT the user prefers, plus the CONTEXT \
(when, why, under what conditions, sentiment strength).

Output a JSON object with one key, ``preferences``, containing an array \
of preference objects. Each preference has:
- "title": 3-7 word noun phrase naming the preference dimension \
  (e.g. "Breakfast preference", "Coffee in morning", "Reading material")
- "content": ONE declarative sentence stating the user's preference \
  with FULL CONTEXT inline. Capture: WHAT they prefer (the object), \
  WHEN/WHERE (qualifier), WHY (if stated), and STRENGTH (strong / mild \
  / context-dependent). Examples:
  - "User prefers oatmeal with berries for breakfast on busy mornings, \
     citing time efficiency."
  - "User strongly prefers Dr. Patel's clinic over their previous \
     clinic because the staff was more attentive."
  - "User prefers Sony cameras for travel photography over Nikon, \
     mainly for sensor performance in low light."
- "source_fact_ids": array of strings — the raw fact_id values from \
  the input that contributed to this consolidated preference.

Rules:
- DO NOT invent preferences not stated in the input. If the user only \
  said "I like X", that's a preference; if they merely mentioned X, \
  that's NOT a preference.
- Merge multiple raw mentions of the SAME preference into one \
  consolidated row. Cite all contributing fact_ids.
- If two raw preferences CONTRADICT (e.g. "I used to prefer X, now I \
  prefer Y"), produce ONE row reflecting the LATEST state, with \
  "(previously X)" inline.
- Skip vague / fleeting statements ("kind of liked it"). Only \
  consolidate STABLE preferences.
- If the input has fewer than 2 distinct preferences, return \
  ``{{"preferences": []}}``.

Input preferences (raw extractions):
{prefs_block}

OUTPUT MUST BE VALID JSON. No prose around it.
"""


def _slugify(text: str) -> str:
    """3-7 word title → URL-safe slug. Same shape as section_compile."""
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s[:60] or "pref"


def _format_pref_for_prompt(fact: PageIndexFact) -> str:
    """Render one preference fact for the consolidation prompt."""
    parts = [f"id={fact.id}"]
    if fact.entities:
        parts.append(f"entities={','.join(fact.entities[:6])}")
    return f"[{', '.join(parts)}] {fact.text}"


async def compile_preferences_for_document(
    *,
    mental_model_store: MentalModelStore,
    bank_id: str,
    document_id: str,
    facts: list[PageIndexFact],
    provider: LLMProvider,
    model: str | None = None,
    max_preferences: int = 12,
) -> list[str]:
    """Consolidate preference-type facts → preference-kind MentalModels.

    Args:
        mental_model_store: where to persist the consolidated models.
        bank_id: tenant scope.
        document_id: scope for the resulting models
            (``scope='document:<id>'``).
        facts: ALL facts extracted for this document — the function
            filters internally for ``fact_type=='preference'``. Passed
            from the caller's retain path to avoid an extra round trip
            to the store.
        provider: LLM provider for the consolidation call.
        model: LLM model name (defaults to provider's default —
            typically gpt-4o-mini for our bench).
        max_preferences: cap on consolidated rows produced. Same cap
            shape as ``mental_model_compile``.

    Returns:
        List of ``model_id`` values for the rows actually persisted.
        Empty list when there were insufficient preference facts or
        the LLM produced no usable output.

    Idempotent: if preference-kind models already exist for this
    ``(bank_id, document_id)`` scope, skip the compile. Re-running on a
    cached bank is therefore a no-op — fresh banks pay the LLM call.
    """
    pref_facts = [f for f in facts if f.fact_type == "preference"]
    if len(pref_facts) < 2:
        _logger.debug(
            "preference_compile: doc=%s has %d preference facts (<2), skip",
            document_id,
            len(pref_facts),
        )
        return []

    # Idempotency check — skip if we already have preference models for this scope.
    try:
        existing = await mental_model_store.list(
            bank_id,
            scope=f"document:{document_id}",
            kind="preference",
        )
    except TypeError:
        # Older MentalModelStore SPI without `kind` kwarg — fall back to
        # listing all and filtering client-side. Lets this module work
        # against not-yet-migrated stores (some tests use older fakes).
        all_for_scope = await mental_model_store.list(
            bank_id,
            scope=f"document:{document_id}",
        )
        existing = [m for m in all_for_scope if m.kind == "preference"]
    if existing:
        _logger.debug(
            "preference_compile: doc=%s already has %d preference models — skip",
            document_id,
            len(existing),
        )
        return [m.model_id for m in existing]

    prefs_block = "\n".join(_format_pref_for_prompt(f) for f in pref_facts)
    msg = _COMPILE_PROMPT.format(
        max_prefs=max_preferences,
        prefs_block=prefs_block,
    )

    try:
        # max_tokens=800 (M14.6) truncated JSON output mid-string on ~90% of
        # LME documents (M14.7-b1.1 diagnostic 2026-05-13). At
        # max_preferences=12, each preference body runs ~150-200 tokens of
        # structured JSON, so 800 was undersized by ~2.5×. The truncation
        # caused entire documents to land with 0 preference-models, which
        # silently neutered the B-1 anchor pool for downstream questions.
        # Bumped to 3000 with margin for ``source_fact_ids`` arrays.
        completion = await provider.complete(
            [Message(role="user", content=msg)],
            model=model,
            max_tokens=3000,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "preference_compile.llm: call failed for doc=%s (%s)",
            document_id,
            exc,
        )
        return []

    try:
        data = json.loads(completion.text)
    except (json.JSONDecodeError, AttributeError) as exc:
        _logger.warning(
            "preference_compile.parse: bad JSON for doc=%s (%s) text=%r",
            document_id,
            exc,
            getattr(completion, "text", "")[:200],
        )
        return []

    items = data.get("preferences") or []
    if not isinstance(items, list):
        return []

    now = datetime.now(tz=timezone.utc)
    scope = f"document:{document_id}"
    saved: list[str] = []
    seen_ids: set[str] = set()

    # M40 — index source facts by fact_id so we can pull per-source
    # evidence timestamps when building each preference's MentalModel.
    # Preference order: mentioned_at (when the user said it) > occurred_start
    # (when the event occurred) > now (last resort — only for facts that
    # carry no temporal info at all, which would trend NEW from any
    # reference_date in the conversation timeline).
    _fact_by_id = {getattr(f, "fact_id", None): f for f in pref_facts}

    def _ts_for_fact_id(fid: str) -> datetime:
        f = _fact_by_id.get(fid)
        if f is None:
            return now
        return (
            getattr(f, "mentioned_at", None)
            or getattr(f, "occurred_start", None)
            or now
        )

    for raw in items[:max_preferences]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title", "")).strip()
        content = str(raw.get("content", "")).strip()
        if not title or not content:
            continue
        source_fact_ids = raw.get("source_fact_ids") or []
        if not isinstance(source_fact_ids, list):
            source_fact_ids = []
        source_fact_ids = [str(s) for s in source_fact_ids if isinstance(s, (str, int))]
        # Stable model_id: pref:<doc_short>:<title-slug>
        slug = _slugify(title)
        model_id = f"pref:{document_id[:8]}:{slug}"
        if model_id in seen_ids:
            continue
        seen_ids.add(model_id)

        # M40 — build positionally-aligned timestamp list (one per source_id).
        source_timestamps = [_ts_for_fact_id(sid) for sid in source_fact_ids]

        mm = MentalModel(
            model_id=model_id,
            bank_id=bank_id,
            title=title,
            content=content,
            scope=scope,
            source_ids=source_fact_ids,
            revision=1,
            refreshed_at=now,
            kind="preference",
            source_timestamps=source_timestamps,
        )
        try:
            await mental_model_store.upsert(mm, bank_id)
            saved.append(model_id)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "preference_compile.save: doc=%s id=%s failed (%s)",
                document_id,
                model_id,
                exc,
            )

    _logger.info(
        "preference_compile: doc=%s consolidated %d preferences from %d raw facts",
        document_id,
        len(saved),
        len(pref_facts),
    )
    return saved
