"""M17 follow-up — directive-style preference compile.

Distils raw preference-type facts into a small set of imperative
directives stored as :class:`MentalModel` rows with ``kind="directive"``.
Hindsight-parity: their ``mental_models.subtype='directive'`` rows are
user-curated hard rules that the answerer treats as authoritative;
auto-extracted preferences (subtype='preference') are advisory and
rarely surfaced verbatim in the system prompt.

Why a separate "directive" tier rather than re-enabling the M14.6
preference surface that M14.7 reverted:

- M14.6 surfaced every consolidated preference (typically 8-12 rows
  per document) on EVERY question. That bloated the system prompt and
  caused cross-category regression on LME (single-session-user dropped
  5/5 → 1/5 as the answerer was overwhelmed by personalization context
  on non-preference questions).
- Hindsight's fix is to keep the consolidation pass (subtype='preference',
  advisory) AND add a tight "directive" tier capped at 3-5 rows of the
  most-confident, most-stable preferences. Only the directive tier
  surfaces verbatim. Result: the answerer gets sparse, high-confidence
  hard rules, and the verbose preference tier stays in the store for
  on-demand recall.

Algorithm (one LLM call per document):

1. Pull all ``fact_type='preference'`` PageIndexFacts for the document.
2. If fewer than 2 preference facts, skip (no consolidation signal —
   single-mention preferences are too noisy to promote to directives).
3. Send to an LLM with a prompt asking for **up to 5** imperative
   directives, each with title + content phrased as a short rule the
   answerer can follow verbatim. The prompt explicitly rejects vague
   or single-mention preferences and asks the model to abstain when no
   stable signal exists.
4. Save each as a :class:`MentalModel` with ``kind='directive'``,
   ``scope=f'document:{document_id}'``. Idempotent — skip when the
   document already has directive models.

The bench's :meth:`AstrocyteClient.get_user_profile` surfaces both
``kind='general'`` and ``kind='directive'`` models in the
``## User Profile`` block. ``kind='preference'`` rows remain in the
store but are NOT surfaced in the profile (M14.7 revert preserved).

Cost: 1 LLM call per document (gpt-4o-mini), ~$0.01. Marginal.

See:
- docs/_design/m17-pageindex-ingestion.md (Conversation-Engine bench)
- ``astrocyte.pipeline.preference_compile`` — sibling that produces the
  larger ``kind='preference'`` pool this directive pass distils from
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

_logger = logging.getLogger("astrocyte.pipeline.directive_compile")


_COMPILE_PROMPT = """\
You are extracting a SMALL SET of HARD-RULE DIRECTIVES from a user's \
stated preferences in a conversation transcript. The directives will be \
injected verbatim into an answering assistant's system prompt — treat \
them as authoritative rules the assistant must honour when answering \
follow-up questions.

Output a JSON object with one key, ``directives``, containing an array \
of AT MOST 5 directive objects. Each directive has:
- "title": 2-5 word noun phrase naming the directive (e.g. \
"Breakfast preference", "Dance studio")
- "content": ONE imperative sentence the assistant can follow. \
Use the form "Prefer X over Y because Z." or "Avoid X; the user dislikes \
it." or "Recommend X for context Z." Always include the specific entity \
the user named. Keep under 25 words.
- "source_fact_ids": array of strings — the contributing raw fact_ids.

STRICT RULES:
- Emit AT MOST 5 directives. Quality over quantity. Sparse is better \
than verbose: surface only the most CONFIDENT, STABLE preferences.
- {min_facts_rule}
- Phrase content imperatively (do/don't, prefer/avoid). The assistant \
will read it as a rule.
- DO NOT include fleeting / weak statements ("kind of liked it"). \
Only stable preferences with explicit positive/negative sentiment.
- {empty_input_rule}

Input preferences (raw extractions):
{prefs_block}

OUTPUT MUST BE VALID JSON. No prose around it.
"""

_MIN_FACTS_RULE_MULTI = (
    "A directive must be backed by AT LEAST 2 distinct raw preference "
    "facts in the input. Drop single-mention preferences entirely."
)
_MIN_FACTS_RULE_SINGLE = (
    "This document has only ONE session — single-mention preferences "
    "are admissible because no repeat signal is possible. Emit a "
    "directive for any clearly-stated preference even if it appears "
    "only once, as long as it carries explicit positive/negative "
    'sentiment (e.g. "no screens after 9:30pm", "avoid Y", '
    '"prefer X"). Skip only vague / fleeting statements.'
)
_EMPTY_RULE_MULTI = (
    "If the input has fewer than 2 distinct stable preferences, "
    'return ``{{"directives": []}}``. Returning zero is correct '
    "when the signal is absent."
)
_EMPTY_RULE_SINGLE = (
    "If the input has no clearly-stated preferences, return "
    '``{{"directives": []}}``. Returning zero is correct when the '
    "signal is absent."
)


_MAX_DIRECTIVES = 5
_MIN_FACTS_TO_COMPILE = 2


def _slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s[:60] or "dir"


def _format_pref_for_prompt(fact: PageIndexFact) -> str:
    parts = [f"id={fact.id}"]
    if fact.entities:
        parts.append(f"entities={','.join(fact.entities[:6])}")
    return f"[{', '.join(parts)}] {fact.text}"


async def compile_directives_for_document(
    *,
    mental_model_store: MentalModelStore,
    bank_id: str,
    document_id: str,
    facts: list[PageIndexFact],
    provider: LLMProvider,
    model: str | None = None,
    max_directives: int = _MAX_DIRECTIVES,
    n_sessions: int | None = None,
) -> list[str]:
    """Consolidate preference facts → at most ``max_directives`` directive
    mental models. Idempotent: skips when directive models already exist
    for the document.

    ``n_sessions`` is the document's session count (caller-supplied so
    we don't need a store round-trip). When ``n_sessions == 1`` we lower
    the minimum-facts threshold to 1: in a single-session document, the
    ≥2-distinct-facts heuristic silently drops every single-mention
    preference (e.g. "no screens after 9:30pm" said once), which is the
    only signal we have for that document. The cross-document corpus
    that motivated the ≥2 threshold doesn't apply when there IS only
    one session.

    Returns the list of ``model_id`` values for rows persisted (or
    already-present on repeat call).
    """
    pref_facts = [f for f in facts if f.fact_type == "preference"]
    # Fix 2 (conv-run-4): single-session docs lower the threshold to 1
    # so single-mention preferences make it to a directive instead of
    # being dropped silently. The ≥2 threshold is a noise-reduction
    # heuristic for multi-session corpora — irrelevant when only one
    # session's worth of preferences exists for this document.
    is_single_session = n_sessions is not None and n_sessions <= 1
    min_threshold = 1 if is_single_session else _MIN_FACTS_TO_COMPILE
    if len(pref_facts) < min_threshold:
        _logger.debug(
            "directive_compile: doc=%s has %d preference facts (<%d, single_session=%s), skip",
            document_id,
            len(pref_facts),
            min_threshold,
            is_single_session,
        )
        return []

    # Idempotency — skip if directives already exist for this document scope.
    try:
        existing = await mental_model_store.list(
            bank_id,
            scope=f"document:{document_id}",
            kind="directive",
        )
    except TypeError:
        all_for_scope = await mental_model_store.list(
            bank_id,
            scope=f"document:{document_id}",
        )
        existing = [m for m in all_for_scope if getattr(m, "kind", None) == "directive"]
    if existing:
        return [m.model_id for m in existing]

    prefs_block = "\n".join(_format_pref_for_prompt(f) for f in pref_facts)
    msg = _COMPILE_PROMPT.format(
        prefs_block=prefs_block,
        min_facts_rule=(_MIN_FACTS_RULE_SINGLE if is_single_session else _MIN_FACTS_RULE_MULTI),
        empty_input_rule=(_EMPTY_RULE_SINGLE if is_single_session else _EMPTY_RULE_MULTI),
    )

    try:
        completion = await provider.complete(
            [Message(role="user", content=msg)],
            model=model,
            max_tokens=1200,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "directive_compile.llm: call failed doc=%s (%s)",
            document_id,
            exc,
        )
        return []

    try:
        data = json.loads(completion.text)
    except (json.JSONDecodeError, AttributeError) as exc:
        _logger.warning(
            "directive_compile.parse: bad JSON doc=%s (%s) text=%r",
            document_id,
            exc,
            getattr(completion, "text", "")[:200],
        )
        return []

    items = data.get("directives") or []
    if not isinstance(items, list):
        return []

    now = datetime.now(tz=timezone.utc)
    scope = f"document:{document_id}"
    saved: list[str] = []
    seen_ids: set[str] = set()

    # M40 — index source facts so MM construction can attach per-source
    # evidence timestamps in conversation time (mirrors preference_compile).
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

    for raw in items[:max_directives]:
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
        slug = _slugify(title)
        model_id = f"dir:{document_id[:8]}:{slug}"
        if model_id in seen_ids:
            continue
        seen_ids.add(model_id)

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
            kind="directive",
            source_timestamps=source_timestamps,
        )
        try:
            await mental_model_store.upsert(mm, bank_id)
            saved.append(model_id)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "directive_compile.save: doc=%s id=%s failed (%s)",
                document_id,
                model_id,
                exc,
            )

    _logger.info(
        "directive_compile: doc=%s produced %d directives from %d preference facts",
        document_id,
        len(saved),
        len(pref_facts),
    )
    return saved
