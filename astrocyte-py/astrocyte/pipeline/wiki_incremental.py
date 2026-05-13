"""M14.2: Karpathy update-affected-wikis.

When a new document is retained, identify existing wiki pages whose
provenance entities overlap with the new document's entities, then run
an LLM update pass per affected wiki with the new content folded in.
Models Karpathy's LLM-wiki spec ("update-affected-wikis" op) and
Hindsight's observation-consolidation freshness pattern — both share
the shape: detect what changed, ripple to dependent compiled artefacts.

Why this is the next M14 lever after M14.6 revert:

The post-M14.7-revert decision was: stop auto-injecting preferences
into the answerer's context (Hindsight design); double down on the
consolidation pipeline so retrieved candidates ARE fresh. M14.2
attacks that directly:

1. **knowledge-update** category on LME (current 5/5 at top_20 — already
   strong, but only because wikis happen to recompile from scratch each
   bench run). Real users add documents incrementally; stale wikis
   would otherwise hurt knowledge-update over time.
2. **multi-session** on LME — questions that span sessions need
   cross-document wiki updates to surface coherent state. Currently
   wikis are document-local; M14.2 enables document-spanning updates.
3. **open-domain** + **temporal** on LoCoMo — both benefit from
   freshness signal in the wiki content (e.g. "user now lives in
   Berlin (previously Paris)" inline in the relevant wiki).

Algorithm:

::

    on_document_retained(doc):
      affected = find_affected_wikis(doc.entities)   # entity overlap
      for wiki in affected:
          ctx  = (wiki.content, new_doc.relevant_sections)
          out  = llm.update_wiki(ctx)               # one call per wiki
          if out.verdict == "UPDATE":
              save_revision(wiki, out.new_content)

The "find_affected_wikis" step is a cheap SQL join via
``astrocyte_pi_wiki_provenance`` × ``astrocyte_pi_section_entities``
(both already populated by M9 + M12.5).

Cost: ~$0.05 per source (one LLM call per affected wiki, typically
2-5 wikis per source). At LoCoMo 200 docs that's ~$1-2 per bench run.
Marginal vs the answerer's per-question cost.

Design references:
- ``docs/_design/llm-wiki-compile.md`` (Karpathy spec)
- ``docs/_design/m13-m14-roadmap.md`` §4 (M14.2 row)
- Hindsight: ``hindsight_api/engine/consolidation/consolidator.py``
  (similar shape: identify novel facts → update or create observations)

Status: SKELETON — interface + entity-overlap query + LLM prompt
designed; ``update_affected_wikis_for_document`` is the entry point.
The orchestration wiring into retain (FSM state ``UPDATE_AFFECTED_WIKIS``
in the M14.0 scaffold, or direct call from ``bench_pageindex_locomo``)
is the next implementation step.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from astrocyte.types import Message

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider, PageIndexStore
    from astrocyte.types import WikiPage

_logger = logging.getLogger("astrocyte.pipeline.wiki_incremental")


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Minimum entities shared between new doc and wiki provenance before we
#: consider the wiki "affected". 1 is the loosest threshold; raising to 2
#: cuts spurious cross-doc updates but misses true single-entity-overlap
#: updates. Calibrated against LoCoMo's entity density (~5-15 unique
#: entities per session) — 1 is right for the bench.
MIN_ENTITY_OVERLAP: int = 1

#: Cap on wikis updated per retain. If a single source touches >N wikis
#: we update only the top-N by overlap count — keeps cost bounded.
MAX_AFFECTED_WIKIS_PER_SOURCE: int = 8

#: max_tokens for the update LLM call. Sized for a 1-3 paragraph wiki
#: with revision notes; ample margin for context preservation.
UPDATE_LLM_MAX_TOKENS: int = 1500


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class WikiUpdateResult:
    """One wiki's update outcome from an incremental update pass."""

    page_id: str
    verdict: str  # 'UPDATED' | 'NO_CHANGE' | 'FAILED'
    new_revision: int | None = None
    detail: str = ""


@dataclass
class IncrementalUpdateReport:
    """Per-document aggregate of wiki update outcomes."""

    document_id: str
    affected_count: int  # how many wikis the entity-overlap query found
    updated: list[WikiUpdateResult] = field(default_factory=list)
    skipped: list[WikiUpdateResult] = field(default_factory=list)
    failed: list[WikiUpdateResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------


_UPDATE_PROMPT = """\
You are maintaining a knowledge wiki. A new conversation has been retained \
that overlaps with this existing wiki entry. Decide whether the new \
content REQUIRES the wiki to be updated, or whether the existing wiki \
already captures the new information (no change needed).

Existing wiki (title: "{title}", revision {revision}):
=== BEGIN WIKI ===
{wiki_content}
=== END WIKI ===

New content from the retained conversation (chronologically AFTER the \
wiki's existing provenance):
=== BEGIN NEW CONTENT ===
{new_content}
=== END NEW CONTENT ===

Rules:
- The wiki tracks LATEST state. If the new content updates a fact \
  the wiki currently states (e.g. user moved cities, changed jobs, \
  finished a project they were planning), emit UPDATE with the \
  revised content reflecting the new state. Preserve the previous \
  state inline as parenthetical when historically relevant (e.g. \
  "(previously lived in Paris)").
- If the new content adds NEW information not covered by the wiki, \
  emit UPDATE with the addition integrated naturally.
- If the new content merely re-states what the wiki already says, \
  emit NO_CHANGE.
- If the new content is unrelated to the wiki's topic despite the \
  entity overlap (e.g. shared person name in different contexts), \
  emit NO_CHANGE.
- Preserve the wiki's voice and structure. Output the FULL revised \
  content, not a diff.

Output a JSON object:
{{
  "verdict": "UPDATE" or "NO_CHANGE",
  "revised_content": "<full revised wiki content; only when UPDATE>",
  "revised_title": "<optional new title; only when UPDATE and title needs change>"
}}

OUTPUT MUST BE VALID JSON. No prose around it.
"""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def update_affected_wikis_for_document(  # noqa: PLR0913
    *,
    page_index_store: PageIndexStore,
    provider: LLMProvider,
    bank_id: str,
    document_id: str,
    new_entities: list[str],
    new_content_excerpts: dict[str, str],
    model: str | None = None,
    min_overlap: int = MIN_ENTITY_OVERLAP,
    max_updates: int = MAX_AFFECTED_WIKIS_PER_SOURCE,
) -> IncrementalUpdateReport:
    """Run Karpathy update-affected-wikis for one retained source.

    Args:
        page_index_store: source of the entity-overlap query AND wiki
            persistence (``save_wiki_page`` for revised wikis).
        provider: LLM provider for the update calls.
        bank_id: tenant scope.
        document_id: the newly-retained document whose entities trigger
            the update sweep.
        new_entities: distinct entity names extracted from the new
            document (already deduped by caller).
        new_content_excerpts: ``entity_name → relevant excerpt`` from
            the new document; passed to the update LLM as evidence.
        model: LLM model name (defaults to provider's default).
        min_overlap: minimum shared entities to flag a wiki as affected.
        max_updates: cap on update calls (top-N by overlap count).

    Returns:
        :class:`IncrementalUpdateReport` with per-wiki outcomes.

    Idempotent across re-runs: the second invocation finds the wiki's
    revision already reflects the new state and emits NO_CHANGE.
    """
    report = IncrementalUpdateReport(
        document_id=document_id, affected_count=0,
    )

    if not new_entities:
        _logger.debug(
            "wiki_incremental: doc=%s has no entities — skip",
            document_id,
        )
        return report

    # ── Find affected wikis via entity overlap (SPI returns full WikiPage rows) ──
    try:
        affected = await page_index_store.list_wikis_affected_by_entities(
            bank_id, list(new_entities),
            min_overlap=min_overlap,
            limit=max_updates,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "wiki_incremental.spi: list_wikis_affected_by_entities "
            "failed doc=%s (%s)",
            document_id, exc,
        )
        return report

    report.affected_count = len(affected)
    if not affected:
        _logger.debug(
            "wiki_incremental: doc=%s no wikis affected by entities=%s",
            document_id, new_entities[:5],
        )
        return report

    # ── Per-wiki update calls ────────────────────────────────────────
    for wiki, _overlap_count, shared_entities in affected:
        result = await _update_one_wiki(
            wiki=wiki,
            page_index_store=page_index_store,
            provider=provider,
            shared_entities=shared_entities,
            new_content_excerpts=new_content_excerpts,
            model=model,
        )
        if result.verdict == "UPDATED":
            report.updated.append(result)
        elif result.verdict == "NO_CHANGE":
            report.skipped.append(result)
        else:
            report.failed.append(result)

    _logger.info(
        "wiki_incremental: doc=%s affected=%d updated=%d skipped=%d failed=%d",
        document_id, report.affected_count,
        len(report.updated), len(report.skipped), len(report.failed),
    )
    return report


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _update_one_wiki(  # noqa: PLR0913
    *,
    wiki: WikiPage,
    page_index_store: PageIndexStore,
    provider: LLMProvider,
    shared_entities: list[str],
    new_content_excerpts: dict[str, str],
    model: str | None,
) -> WikiUpdateResult:
    """Run one update prompt and persist the result.

    Composes ``new_content`` as the union of excerpts keyed by
    entities the wiki and new doc share, so the LLM only sees evidence
    relevant to this wiki's topic.
    """
    new_content_parts = [
        f"[{e}] {new_content_excerpts.get(e, '').strip()}"
        for e in shared_entities
        if new_content_excerpts.get(e, "").strip()
    ]
    if not new_content_parts:
        return WikiUpdateResult(
            page_id=wiki.page_id, verdict="NO_CHANGE",
            detail="no excerpt content for shared entities",
        )
    new_content = "\n\n".join(new_content_parts)

    msg = _UPDATE_PROMPT.format(
        title=wiki.title, revision=wiki.revision,
        wiki_content=wiki.content, new_content=new_content,
    )
    try:
        completion = await provider.complete(
            [Message(role="user", content=msg)],
            model=model,
            max_tokens=UPDATE_LLM_MAX_TOKENS,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "wiki_incremental.llm: call failed for page=%s (%s)",
            wiki.page_id, exc,
        )
        return WikiUpdateResult(
            page_id=wiki.page_id, verdict="FAILED", detail=str(exc),
        )

    try:
        data = json.loads(completion.text)
    except (json.JSONDecodeError, AttributeError) as exc:
        _logger.warning(
            "wiki_incremental.parse: bad JSON for page=%s (%s)",
            wiki.page_id, exc,
        )
        return WikiUpdateResult(
            page_id=wiki.page_id, verdict="FAILED",
            detail=f"bad JSON: {exc}",
        )

    verdict = str(data.get("verdict", "")).upper()
    if verdict == "NO_CHANGE":
        return WikiUpdateResult(page_id=wiki.page_id, verdict="NO_CHANGE")

    if verdict != "UPDATE":
        return WikiUpdateResult(
            page_id=wiki.page_id, verdict="FAILED",
            detail=f"unrecognised verdict: {verdict!r}",
        )

    revised_content = (data.get("revised_content") or "").strip()
    if not revised_content:
        return WikiUpdateResult(
            page_id=wiki.page_id, verdict="FAILED",
            detail="UPDATE verdict with empty revised_content",
        )
    revised_title = (data.get("revised_title") or wiki.title).strip()

    # Save as a new revision via ``PageIndexStore.save_wiki_page`` — its
    # upsert semantics auto-bump revision and archive the prior version.
    # The wiki's existing provenance is preserved: we re-parse it from
    # ``source_ids`` (M12.6 format: ``"<doc_id>:<line_num>"``) if present;
    # otherwise we pass an empty list (provenance row already exists in
    # ``astrocyte_pi_wiki_provenance`` from the initial compile and is
    # untouched by content-only revisions).
    from dataclasses import replace  # noqa: PLC0415
    new_wiki = replace(
        wiki,
        title=revised_title,
        content=revised_content,
        revision=wiki.revision + 1,
        revised_at=datetime.now(tz=timezone.utc),
    )
    provenance: list[tuple[str, int]] = []
    for sid in wiki.source_ids or []:
        if ":" in sid:
            doc_id, _, line_str = sid.rpartition(":")
            try:
                provenance.append((doc_id, int(line_str)))
            except ValueError:
                continue
    try:
        await page_index_store.save_wiki_page(
            page=new_wiki, embedding=None, provenance=provenance,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "wiki_incremental.save: page=%s revision=%d failed (%s)",
            wiki.page_id, new_wiki.revision, exc,
        )
        return WikiUpdateResult(
            page_id=wiki.page_id, verdict="FAILED",
            detail=f"save failed: {exc}",
        )

    return WikiUpdateResult(
        page_id=wiki.page_id, verdict="UPDATED",
        new_revision=new_wiki.revision,
    )
