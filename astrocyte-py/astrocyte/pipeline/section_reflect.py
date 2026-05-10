"""Section reflect adapter (M9 PR2.6).

Bridges the section recall pipeline (``section_recall``,
``expand_section_links``) into the agentic reflect loop's MemoryHit-
shaped tool surface. Reflect doesn't know about sections — it sees
memories with ``id`` / ``text`` / ``score``.

The conversion convention:
- ``MemoryHit.memory_id = f"{document_id}:{line_num}"``
- ``MemoryHit.text`` is a windowed slice of the markdown around the
  section's line_num (same slicer the bench's synth uses, kept short
  enough that ~30 hits fit the agent's context budget).
- ``MemoryHit.score`` carries the upstream score (rrf / cosine /
  link weight) verbatim — useful for the agent to triage results.

Two factories produce closures the reflect loop can call:

- :func:`make_section_recall_fn` — runs ``section_recall`` on demand for
  a sub-query the agent issues. Mode is fixed to ``"default"`` so all
  always-on strategies fire (semantic + keyword + entity); the agent
  refines by query text, not by mode.
- :func:`make_section_expand_fn` — given a section memory_id, calls
  ``expand_section_links`` for 1-hop graph expansion. Counting /
  multi-session benefit most: "I see one mention, give me adjacent
  sections."

Why this exists: PR2.5 (counting synth) showed the picker undercounts
when multi-session aggregation is required — fetches 5-6 sections when
the answer needs 8-10. Reflect's iterative tool-call loop resolves
that by letting the agent re-query until it has enough evidence.
See ``docs/_design/recall.md`` §7 (PR2.6 reflect dispatch).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from astrocyte.types import MemoryHit

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider, PageIndexStore

_logger = logging.getLogger("astrocyte.pipeline.section_reflect")


# ── Memory-id conventions ───────────────────────────────────────────


def format_section_memory_id(document_id: str, line_num: int) -> str:
    """Compose a ``MemoryHit.memory_id`` from a (doc, line) pair.

    Used everywhere a section is exposed to the reflect agent so the
    agent's ``cited_ids`` can be parsed back deterministically."""
    return f"{document_id}:{line_num}"


def parse_section_memory_id(memory_id: str) -> tuple[str, int] | None:
    """Inverse of :func:`format_section_memory_id`. Returns ``None``
    when the id doesn't conform — caller decides whether to skip or
    raise. The reflect loop already validates citations against the
    seen-id pool, so an out-of-shape id here means the agent invented
    one (filter, don't crash)."""
    if not memory_id:
        return None
    sep = memory_id.rfind(":")
    if sep < 0:
        return None
    doc_id = memory_id[:sep]
    try:
        line_num = int(memory_id[sep + 1 :])
    except ValueError:
        return None
    if not doc_id:
        return None
    return doc_id, line_num


# ── Section → MemoryHit conversion ──────────────────────────────────


def section_tuples_to_memory_hits(
    tuples: list[tuple[str, int, float]],
    *,
    md_text_by_doc: dict[str, str],
    slice_fn: Callable[[str, int], str],
    max_chars: int = 600,
) -> list[MemoryHit]:
    """Convert ``[(doc_id, line_num, score), ...]`` to MemoryHits.

    ``slice_fn(md_text, line_num) -> str`` is the bench's section
    slicer (kept caller-supplied so this module doesn't take a hard
    dep on the bench file's slicing rules). Truncates each hit's text
    to ``max_chars`` so 20-30 hits comfortably fit the agent's
    1024-token reply window when round-tripped as tool results.
    """
    out: list[MemoryHit] = []
    for doc_id, line_num, score in tuples:
        md = md_text_by_doc.get(doc_id, "")
        text = slice_fn(md, line_num) if md else ""
        if len(text) > max_chars:
            text = text[: max_chars - 3] + "..."
        out.append(MemoryHit(
            text=text,
            score=float(score),
            memory_id=format_section_memory_id(doc_id, line_num),
        ))
    return out


# ── Closure factories for the reflect loop ─────────────────────────


RecallFn = Callable[[str, int], Awaitable[list[MemoryHit]]]
ExpandFn = Callable[[str, int], Awaitable[list[MemoryHit]]]


def make_section_recall_fn(
    *,
    store: PageIndexStore,
    bank_id: str,
    embedding_provider: LLMProvider,
    md_text_by_doc: dict[str, str],
    slice_fn: Callable[[str, int], str],
    sub_recall_mode: str = "default",
) -> RecallFn:
    """Build a ``recall_fn(query, max_results) -> [MemoryHit]`` that
    re-runs section recall on the agent's sub-query.

    Mode is fixed (default ``"default"``) so the reflect loop's
    sub-queries always exercise the always-on baseline strategies —
    we don't try to re-detect mode per sub-query. The agent refines
    via query text.
    """
    # Local import to keep the module pure (no top-level dep on
    # section_recall — callers without pgvector still parse it cleanly).
    from astrocyte.pipeline.section_recall import section_recall

    async def _recall(query: str, max_results: int) -> list[MemoryHit]:
        try:
            result = await section_recall(
                store=store,
                bank_id=bank_id,
                question=query,
                mode=sub_recall_mode,
                embedding_provider=embedding_provider,
                # No annotator on sub-queries — the agent's query text
                # IS the refinement signal. Pre-extracted entities /
                # date_range from the outer pass would only match the
                # original question's context.
                question_entities=None,
                date_range=None,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "section_reflect.recall_fn failed for q=%r: %s: %s",
                query[:60], type(exc).__name__, exc,
            )
            return []
        # Promote the top-N fused hits into MemoryHit shape. We don't
        # rerun the cross-encoder reranker here — the agent's
        # iterative loop is itself a form of reranking, and avoiding
        # the model load keeps each tool call cheap.
        tuples = [
            (h.document_id, h.line_num, h.rrf_score)
            for h in result.fused[:max_results]
        ]
        return section_tuples_to_memory_hits(
            tuples,
            md_text_by_doc=md_text_by_doc,
            slice_fn=slice_fn,
        )

    return _recall


def make_section_expand_fn(
    *,
    store: PageIndexStore,
    md_text_by_doc: dict[str, str],
    slice_fn: Callable[[str, int], str],
    link_types: list[str] | None = None,
) -> ExpandFn:
    """Build an ``expand_fn(memory_id, max_sources) -> [MemoryHit]``
    that 1-hop expands a section through ``section_links``.

    ``link_types=None`` returns all link types (causal / supersedes /
    elaborates / semantic_knn). Counting questions benefit most from
    ``semantic_knn`` (sibling sections of a known mention); causal
    questions benefit from causal/supersedes. We default to all and
    let the rrf-style score on the link expose what mattered.
    """

    async def _expand(memory_id: str, max_sources: int) -> list[MemoryHit]:
        parsed = parse_section_memory_id(memory_id)
        if parsed is None:
            _logger.info("section_reflect.expand_fn: unparseable memory_id=%r", memory_id)
            return []
        doc_id, line_num = parsed
        try:
            tuples = await store.expand_section_links(
                [(doc_id, line_num)],
                link_types=link_types,
                top_k=max_sources,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "section_reflect.expand_fn failed: %s: %s",
                type(exc).__name__, exc,
            )
            return []
        return section_tuples_to_memory_hits(
            tuples,
            md_text_by_doc=md_text_by_doc,
            slice_fn=slice_fn,
        )

    return _expand


# ── Citation → line_nums ────────────────────────────────────────────


def cited_ids_to_line_nums(
    cited_memory_ids: list[str],
    *,
    expected_doc_id: str | None = None,
) -> list[int]:
    """Extract line_nums from a reflect ``ReflectResult.sources``.

    ``expected_doc_id`` filters out citations from sibling documents
    (the bench builds one document per question, so cross-doc
    citations indicate the agent confused itself). Pass ``None`` when
    the caller wants all parsed line_nums regardless of doc.
    """
    out: list[int] = []
    seen: set[int] = set()
    for mid in cited_memory_ids:
        parsed = parse_section_memory_id(mid)
        if parsed is None:
            continue
        doc_id, line_num = parsed
        if expected_doc_id is not None and doc_id != expected_doc_id:
            continue
        if line_num in seen:
            continue
        out.append(line_num)
        seen.add(line_num)
    return out
