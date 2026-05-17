"""M10.1 — section-aware consolidation engine.

Hindsight-aligned: aggregation happens at retain time so query-time
agents can find pre-aggregated observations via wiki recall, instead
of trying to count distinct items across many section excerpts (which
the LLM is unreliable at).

Algorithm per document:

1. Load sections with ``summary_embedding`` populated.
2. DBSCAN-cluster by cosine similarity (eps=0.30, min_samples=2 by
   default) — same approach as the M8 ``CompileEngine`` over raw
   memory_units, just at section grain.
3. For each cluster, single LLM call synthesizes a short observation:
   a 5-10 word title and a 1-3 sentence aggregated claim that cites
   specific dates / counts / entities. This is the primitive that
   answers "how many doctors did I visit?" without making the agent
   count across sessions at query time.
4. Save each observation as a :class:`~astrocyte.types.WikiPage`
   (kind="topic"), with provenance back to the source sections via
   ``astrocyte_pi_wiki_provenance`` (migration 015) and an embedding
   on ``astrocyte_wiki_pages.current_embedding`` (migration 018).

Differs from M8's :class:`~astrocyte.pipeline.compile.CompileEngine`:
M8 reads from ``VectorStore`` (memory_units); this module reads from
``PageIndexStore`` (sections). Both produce ``WikiPage`` rows; both
use the same ``_dbscan`` clustering. The split exists because the
PageIndex POC bypasses ``VectorStore`` entirely.

See:
- ``docs/_design/recall.md`` §12 (PR2.6 close-out, M10 plan)
- ``adr/adr-006-three-layer-recall-stack.md`` — Layer 1 wiki tier
- ``hindsight/hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py``
  — Hindsight's analogous flow over raw memories
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from astrocyte.pipeline.compile import _dbscan  # reuse pure-Python DBSCAN
from astrocyte.types import Message, WikiPage

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider, PageIndexStore
    from astrocyte.types import PageIndexSection

_logger = logging.getLogger("astrocyte.pipeline.section_compile")


# ── Synthesis prompt ────────────────────────────────────────────────


_OBSERVATION_PROMPT = """\
You are an observation synthesizer. You will be given several sections \
from a conversation that all discuss the same topic, presented in \
CHRONOLOGICAL ORDER (oldest first). Your job: write ONE concise \
observation that aggregates the user's experience or stable facts \
across these sections, reflecting the LATEST stable state.

Pay attention to LATER sections that may supersede earlier ones. If \
the user changes their mind, switches providers, or adds new items \
over time, the observation should reflect the LATEST state. Capture \
the chronological progression when it matters (e.g. "previously did \
X, now does Y") rather than averaging across time.

Output a JSON object with EXACTLY two fields:
- "title": a 5-10 word summary that uses CATEGORY nouns the reader \
might query (e.g. "Doctors visited", "Model kits worked on", \
"Camping trips taken"). Title should mention the category (doctor / \
kit / trip / restaurant / etc.) so the wiki search can match it.
- "content": 1-3 sentences. MANDATORY: lead with an explicit COUNT \
followed by a colon-separated enumeration. The reader will use your \
content as the answer to questions like "how many X?" / "total Y?" \
/ "list of Z?". DO NOT generalize or theme — enumerate the actual \
distinct items. When listing items added over time, the order is \
chronological (earliest first).

REQUIRED CONTENT SHAPE:
"User <verb>ed N distinct <category>s: <item1> (<context>), \
<item2> (<context>), ..., <itemN> (<context>)."

Examples of good observations:
{{"title": "Doctors visited", "content": "User visited 3 distinct \
doctors: a primary care physician (Dr. Patel, May), an ENT specialist \
(Dr. Lee, June), and a dermatologist (referral, July)."}}

{{"title": "Model kits worked on", "content": "User worked on 5 model \
kits: a Revell F-15 Eagle, a B-29 bomber, a 1/72 Spitfire, a Tiger I \
tank, and a Sherman tank."}}

{{"title": "Camping trips taken", "content": "User went on 4 camping \
trips totaling 8 days: Yosemite (3 days, April), Lake Tahoe (2 days, \
June), Olympic NP (2 days, August), Grand Canyon (1 day, October)."}}

{{"title": "Korean restaurants visited", "content": "User visited 3 \
distinct Korean restaurants: Yeon Su (Chelsea), Cote (Flatiron), and \
Hanjip (LA, during travel)."}}

ANTI-PATTERNS to avoid:
- "User is managing health" (no count, no enumeration) — WRONG
- "User explored various trips" (vague, no specifics) — WRONG
- "User had multiple doctor visits" (says "multiple" without counting) — WRONG

When the cluster has only 1 distinct fact, still lead with "1": \
"User has 1 ongoing project: <description>."

OUTPUT MUST BE VALID JSON. No prose around it.

Sections in this cluster:
{sections}
"""


# ── Cluster helpers ─────────────────────────────────────────────────


def _slugify(s: str) -> str:
    """Convert a title to a URL-safe slug. e.g. 'Doctors visited' -> 'doctors-visited'."""
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s[:60] or "obs"


def _format_section_for_prompt(section: PageIndexSection) -> str:
    date = section.session_date.strftime("%Y-%m-%d") if section.session_date is not None else "no-date"
    summary = (section.summary or section.title or "").strip()
    return f"[line={section.line_num}, date={date}]\n{summary}"


async def _synthesize_observation(
    *,
    provider: LLMProvider,
    model: str | None,
    sections: list[PageIndexSection],
) -> tuple[str, str] | None:
    """LLM call — returns (title, content) or None on parse failure.

    M12.6: sections are sorted by ``session_date`` (with ``line_num``
    fallback) so the synth prompt sees them in chronological order.
    The Karpathy "latest state wins" guidance in the prompt requires
    this ordering to be meaningful.
    """
    if not sections:
        return None
    ordered = sorted(
        sections,
        key=lambda s: (
            s.session_date or datetime.min.replace(tzinfo=timezone.utc),
            s.line_num,
        ),
    )
    rendered = "\n\n".join(_format_section_for_prompt(s) for s in ordered)
    msg = _OBSERVATION_PROMPT.format(sections=rendered)
    try:
        completion = await provider.complete(
            [Message(role="user", content=msg)],
            model=model,
            max_tokens=300,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "section_compile.synthesize: LLM call failed (%s)",
            exc,
        )
        return None
    try:
        data = json.loads(completion.text)
    except json.JSONDecodeError as exc:
        _logger.warning(
            "section_compile.synthesize: JSON parse failed (%s) text=%r",
            exc,
            completion.text[:200],
        )
        return None
    title = str(data.get("title", "")).strip()
    content = str(data.get("content", "")).strip()
    if not title or not content:
        return None
    return title, content


# ── Revision pass (Karpathy incremental update, M12.6) ─────────────


_REVISION_PROMPT = """\
You are revising a knowledge-base entry to reflect the LATEST stable \
state across its source sections.

Current entry (titled "{title}", revision {revision}):
{content}

Source sections in CHRONOLOGICAL ORDER (oldest first):
{sections}

Examine the sections in time order. If a LATER section supersedes, \
contradicts, or extends an earlier claim, the entry should reflect \
the latest state. If the entry already does, no revision is needed.

Common revision triggers:
- Entry says "User visited 3 doctors" but a later section adds a 4th → bump count
- Entry says "User prefers brand X" but later sections say switched to Y → "User now prefers Y (previously X)"
- Entry uses past tense for an ongoing thing → use ongoing tense
- Entry omits items that appear in later sections → add them
- Entry includes superseded items as if current → mark as previous

If revising: the new content follows the SAME shape as initial compile \
(lead with explicit COUNT and colon-separated enumeration; cite dates / \
entities when present).

Respond as JSON only:
{{
  "verdict": "OK" or "REVISE",
  "revised_title": "<5-10 words; only when REVISE>",
  "revised_content": "<1-3 sentences; only when REVISE>"
}}
"""


async def _revise_observation(
    *,
    provider: LLMProvider,
    model: str | None,
    page: WikiPage,
    sections: list[PageIndexSection],
) -> tuple[str, str] | None:
    """LLM call — returns (new_title, new_content) if revised, ``None``
    if the page already reflects the latest state.

    Sections must be sorted chronologically by the caller. Resilient
    to LLM / parse failures (returns ``None``).
    """
    if not sections or not page.content.strip():
        return None
    rendered = "\n\n".join(_format_section_for_prompt(s) for s in sections)
    msg = _REVISION_PROMPT.format(
        title=page.title,
        revision=page.revision,
        content=page.content,
        sections=rendered,
    )
    try:
        completion = await provider.complete(
            [Message(role="user", content=msg)],
            model=model,
            max_tokens=400,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "section_compile.revise: LLM call failed for page=%s (%s)",
            page.page_id,
            exc,
        )
        return None
    try:
        data = json.loads(completion.text)
    except json.JSONDecodeError as exc:
        _logger.warning(
            "section_compile.revise: JSON parse failed for page=%s (%s) text=%r",
            page.page_id,
            exc,
            completion.text[:200],
        )
        return None
    verdict = str(data.get("verdict", "")).strip().upper()
    if verdict != "REVISE":
        return None
    new_title = str(data.get("revised_title", "")).strip()
    new_content = str(data.get("revised_content", "")).strip()
    if not new_title or not new_content:
        return None
    if new_title == page.title and new_content == page.content:
        return None
    return new_title, new_content


async def revise_wikis_for_document(
    *,
    store: PageIndexStore,
    bank_id: str,
    document_id: str,
    provider: LLMProvider,
    model: str | None = None,
    embedding_model: str | None = None,
) -> int:
    """Karpathy-style revision pass — for each existing wiki of this
    document, re-check it against its provenance sections in
    chronological order and persist any revisions.

    Returns: number of pages actually revised.

    Idempotent: if no page needs revision, the call is a no-op (just
    one LLM call per page). Subsequent calls on already-revised pages
    should converge to no-op as the wiki stabilises.
    """
    try:
        pages = await store.list_wiki_pages_for_doc(bank_id, document_id)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "revise_wikis: list_wiki_pages_for_doc failed for doc=%s (%s)",
            document_id,
            exc,
        )
        return 0
    if not pages:
        return 0

    try:
        all_sections = await store.load_sections_with_embeddings(bank_id, document_id)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "revise_wikis: load_sections failed for doc=%s (%s)",
            document_id,
            exc,
        )
        return 0
    section_by_line = {s.line_num: s for s in all_sections}

    revised_count = 0
    for page in pages:
        # Resolve provenance source_ids back to sections.
        provenance_lines: list[int] = []
        for src in page.source_ids or []:
            # source_ids are formatted "docId:lineNum"
            if ":" not in src:
                continue
            _, _, line_str = src.rpartition(":")
            try:
                provenance_lines.append(int(line_str))
            except ValueError:
                continue
        prov_sections = [section_by_line[ln] for ln in provenance_lines if ln in section_by_line]
        if not prov_sections:
            continue
        # Sort chronologically — session_date first, line_num fallback.
        prov_sections.sort(
            key=lambda s: (
                s.session_date or datetime.min.replace(tzinfo=timezone.utc),
                s.line_num,
            ),
        )
        result = await _revise_observation(
            provider=provider,
            model=model,
            page=page,
            sections=prov_sections,
        )
        if result is None:
            continue
        new_title, new_content = result

        # Save as a new revision. ``save_wiki_page`` upserts the page row
        # and inserts a new revision row keyed by (page_uuid, revision_number).
        revised_page = WikiPage(
            page_id=page.page_id,
            bank_id=page.bank_id,
            kind=page.kind,
            title=new_title,
            content=new_content,
            scope=page.scope,
            source_ids=list(page.source_ids or []),
            cross_links=list(page.cross_links or []),
            revision=page.revision + 1,
            revised_at=datetime.now(tz=timezone.utc),
            tags=page.tags,
            metadata=page.metadata,
        )
        try:
            new_embedding = (
                await provider.embed(
                    [f"{new_title}\n\n{new_content}"],
                    model=embedding_model,
                )
            )[0]
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "revise_wikis: embed failed for page=%s (%s)",
                page.page_id,
                exc,
            )
            new_embedding = None

        provenance_pairs = [(s.document_id, s.line_num) for s in prov_sections]
        try:
            await store.save_wiki_page(
                page=revised_page,
                embedding=new_embedding,
                provenance=provenance_pairs,
            )
            revised_count += 1
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "revise_wikis.save failed for page=%s (%s)",
                page.page_id,
                exc,
            )

    if revised_count:
        _logger.info(
            "revise_wikis: doc=%s revised %d/%d pages",
            document_id,
            revised_count,
            len(pages),
        )
    return revised_count


# ── Public entry point ─────────────────────────────────────────────


async def compile_sections_for_document(
    *,
    store: PageIndexStore,
    bank_id: str,
    document_id: str,
    provider: LLMProvider,
    model: str | None = None,
    embedding_model: str | None = None,
    eps: float = 0.55,
    min_samples: int = 2,
    max_pages_per_doc: int = 10,
) -> list[str]:
    """Cluster sections in this document, synthesize an observation per
    cluster, persist as :class:`WikiPage` rows.

    Returns the list of newly-created ``page_id``s. When the document
    already has wiki pages (idempotency check), returns ``[]``.

    ``eps`` / ``min_samples`` tune DBSCAN: defaults match LME-shaped
    chat conversations (sessions are heterogeneous; eps=0.30 forces
    fairly close cosine similarity). Lower ``min_samples`` than M8's
    default (2 vs 3) because section-grain clusters tend to be smaller
    than memory-unit clusters.

    ``max_pages_per_doc`` caps cost: at 5-7 LLM calls per doc × 50 LME
    docs we'd burn ~$2-3 per gate. The cap keeps it bounded if a
    pathological doc clusters into many small groups.
    """
    # Idempotency: skip if pages already exist for this doc.
    try:
        existing = await store.count_wiki_pages_for_doc(bank_id, document_id)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "section_compile.count failed for doc=%s (%s)",
            document_id,
            exc,
        )
        existing = 0
    if existing > 0:
        _logger.debug(
            "section_compile: doc=%s already has %d pages — skip initial compile, run revision pass only",
            document_id,
            existing,
        )
        # M12.6: even when initial compile is skipped, run the Karpathy
        # revision pass against the existing pages. Lets a code change
        # (e.g. new prompt) take effect against cached banks without a
        # full re-extraction.
        try:
            await revise_wikis_for_document(
                store=store,
                bank_id=bank_id,
                document_id=document_id,
                provider=provider,
                model=model,
                embedding_model=embedding_model,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "revise_wikis: cache-hit revision pass failed for doc=%s (%s)",
                document_id,
                exc,
            )
        return []

    # Load sections with embeddings populated.
    sections = await store.load_sections_with_embeddings(bank_id, document_id)
    if not sections:
        return []
    sections_with_emb = [s for s in sections if s.summary_embedding]
    if len(sections_with_emb) < min_samples:
        # Too few embedded sections to cluster meaningfully.
        return []

    # DBSCAN over (line_num_str, embedding) pairs.
    items = [(str(s.line_num), s.summary_embedding) for s in sections_with_emb]
    labels = _dbscan(items, eps=eps, min_samples=min_samples)
    by_cluster: dict[int, list[PageIndexSection]] = {}
    section_by_line = {s.line_num: s for s in sections_with_emb}
    for line_str, cluster_id in labels.items():
        if cluster_id == -1:
            continue
        by_cluster.setdefault(cluster_id, []).append(
            section_by_line[int(line_str)],
        )

    if not by_cluster:
        _logger.debug(
            "section_compile: doc=%s yielded no clusters (eps=%s min=%s)",
            document_id,
            eps,
            min_samples,
        )
        return []

    # Cap cluster count to control cost. Prefer larger clusters (more
    # signal per LLM call).
    clusters = sorted(by_cluster.values(), key=len, reverse=True)[:max_pages_per_doc]

    created_page_ids: list[str] = []
    now = datetime.now(tz=timezone.utc)
    pending_pages: list[WikiPage] = []
    pending_provenance: list[tuple[str, list[tuple[str, int]]]] = []
    pending_summaries: list[str] = []  # used for embedding pass

    for cluster in clusters:
        synth = await _synthesize_observation(
            provider=provider,
            model=model,
            sections=cluster,
        )
        if synth is None:
            continue
        title, content = synth
        page_id = f"obs:{document_id[:8]}:{_slugify(title)}"
        source_ids = [f"{s.document_id}:{s.line_num}" for s in cluster]
        page = WikiPage(
            page_id=page_id,
            bank_id=bank_id,
            kind="topic",
            title=title,
            content=content,
            scope=f"document:{document_id}",
            source_ids=source_ids,
            cross_links=[],
            revision=1,
            revised_at=now,
            tags=None,
            metadata=None,
        )
        pending_pages.append(page)
        provenance_pairs = [(s.document_id, s.line_num) for s in cluster]
        pending_provenance.append((page_id, provenance_pairs))
        # Embed the title + content together so the wiki search
        # strategy matches both the topic label and the aggregated
        # facts. Mirrors how Hindsight indexes observations.
        pending_summaries.append(f"{title}\n\n{content}")

    if not pending_pages:
        return []

    # Single batched embedding call for all observations in this doc.
    try:
        embeddings = await provider.embed(
            pending_summaries,
            model=embedding_model,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "section_compile: embedding batch failed for doc=%s (%s)",
            document_id,
            exc,
        )
        embeddings = [None] * len(pending_pages)

    for page, embedding, (page_id, provenance_pairs) in zip(
        pending_pages,
        embeddings,
        pending_provenance,
    ):
        try:
            await store.save_wiki_page(
                page=page,
                embedding=embedding,
                provenance=provenance_pairs,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "section_compile.save_wiki_page failed page_id=%s (%s)",
                page_id,
                exc,
            )
            continue
        created_page_ids.append(page_id)

    _logger.info(
        "section_compile: doc=%s created %d pages from %d clusters",
        document_id,
        len(created_page_ids),
        len(clusters),
    )

    # M12.6: Karpathy-style revision pass over the freshly compiled
    # pages. Catches "latest state" patterns that single-shot synthesis
    # missed (e.g. counts that grow over time, providers that change).
    # Idempotent — pages already at latest state are unchanged.
    try:
        await revise_wikis_for_document(
            store=store,
            bank_id=bank_id,
            document_id=document_id,
            provider=provider,
            model=model,
            embedding_model=embedding_model,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "revise_wikis: post-initial revision pass failed for doc=%s (%s)",
            document_id,
            exc,
        )

    return created_page_ids
