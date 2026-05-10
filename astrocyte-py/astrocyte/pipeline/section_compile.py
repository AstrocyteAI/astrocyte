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
from a conversation that all discuss the same topic. Your job: write \
ONE concise observation that aggregates the user's experience or \
stable facts across these sections.

Output a JSON object with EXACTLY two fields:
- "title": a 5-10 word summary that uses CATEGORY nouns the reader \
might query (e.g. "Doctors visited", "Model kits worked on", \
"Camping trips taken"). Title should mention the category (doctor / \
kit / trip / restaurant / etc.) so the wiki search can match it.
- "content": 1-3 sentences. MANDATORY: lead with an explicit COUNT \
followed by a colon-separated enumeration. The reader will use your \
content as the answer to questions like "how many X?" / "total Y?" \
/ "list of Z?". DO NOT generalize or theme — enumerate the actual \
distinct items.

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
    date = (
        section.session_date.strftime("%Y-%m-%d")
        if section.session_date is not None
        else "no-date"
    )
    summary = (section.summary or section.title or "").strip()
    return f"[line={section.line_num}, date={date}]\n{summary}"


async def _synthesize_observation(
    *,
    provider: LLMProvider,
    model: str | None,
    sections: list[PageIndexSection],
) -> tuple[str, str] | None:
    """LLM call — returns (title, content) or None on parse failure."""
    if not sections:
        return None
    rendered = "\n\n".join(_format_section_for_prompt(s) for s in sections)
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
            "section_compile.synthesize: LLM call failed (%s)", exc,
        )
        return None
    try:
        data = json.loads(completion.text)
    except json.JSONDecodeError as exc:
        _logger.warning(
            "section_compile.synthesize: JSON parse failed (%s) text=%r",
            exc, completion.text[:200],
        )
        return None
    title = str(data.get("title", "")).strip()
    content = str(data.get("content", "")).strip()
    if not title or not content:
        return None
    return title, content


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
            "section_compile.count failed for doc=%s (%s)", document_id, exc,
        )
        existing = 0
    if existing > 0:
        _logger.debug(
            "section_compile: doc=%s already has %d pages — skip",
            document_id, existing,
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
            document_id, eps, min_samples,
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
            provider=provider, model=model, sections=cluster,
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
            pending_summaries, model=embedding_model,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "section_compile: embedding batch failed for doc=%s (%s)",
            document_id, exc,
        )
        embeddings = [None] * len(pending_pages)

    for page, embedding, (page_id, provenance_pairs) in zip(
        pending_pages, embeddings, pending_provenance,
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
                page_id, exc,
            )
            continue
        created_page_ids.append(page_id)

    _logger.info(
        "section_compile: doc=%s created %d pages from %d clusters",
        document_id, len(created_page_ids), len(clusters),
    )
    return created_page_ids
