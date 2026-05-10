"""Section recall orchestrator (M9 PR2 commit B).

Runs the five Hindsight-pattern parallel strategies — semantic, keyword,
entity, temporal, graph-expand — over the ``PageIndexStore`` SPI, then
fuses their ranked outputs with RRF (Reciprocal Rank Fusion, k=60).

This is the **retrieval** layer. The cross-encoder rerank + picker-as-
reranker step (PR2 commit C) consumes this layer's output and feeds the
synth.

Per-mode strategy gating mirrors what we found in Phase A failure
analysis:
- **temporal questions** add the temporal strategy + a wider semantic
  net (questions mentioning "May 2023" want the May-2023 sessions even
  if the topic words don't match).
- **multi-hop / multi-session questions** add the graph-expand strategy
  to bridge across sessions via section_links.
- **assistant-recall questions** (LME) override the keyword strategy
  with a ``speaker='assistant'`` filter.

Mode dispatch is simple regex/heuristic at PR2 commit B; PR2 commit D
replaces it with a 1-token LLM classifier when the heuristic
mis-routes.

See:
- ``docs/_design/recall.md`` §6 (recall pipeline)
- ``docs/_design/adr/adr-006-three-layer-recall-stack.md``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from astrocyte.pipeline.fusion import DEFAULT_RRF_K

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider, PageIndexStore


# ── Result types ─────────────────────────────────────────────────────


@dataclass
class StrategyResult:
    """One strategy's ranked output, plus the strategy name (for trace)
    and timing (for performance regression detection)."""

    strategy: str
    hits: list[tuple[str, int, float]]
    elapsed_ms: float
    error: str | None = None


@dataclass
class FusedHit:
    """One section after RRF fusion. ``rrf_score`` is the sum of
    1/(k+rank) contributions across strategies that returned it.
    ``per_strategy_rank`` is kept for trace + reranker input."""

    document_id: str
    line_num: int
    rrf_score: float
    per_strategy_rank: dict[str, int] = field(default_factory=dict)


@dataclass
class SectionRecallResult:
    """Full output of one section recall call. Carries per-strategy
    debug data so failure analysis can attribute regressions to the
    right component."""

    fused: list[FusedHit]
    strategies: list[StrategyResult]
    mode: str
    elapsed_ms: float
    # M10.1: wiki page hits found at recall time. Surfaced separately
    # from ``fused`` because they carry pre-aggregated text the bench
    # prepends to synth excerpts as ``[OBSERVATION]`` blocks rather
    # than feeding through the picker. Empty list when no wiki tier or
    # no hits cleared the score threshold.
    wiki_hits: list = field(default_factory=list)


# ── Section-grain RRF fusion (specialised for tuple hits) ─────────────


def _rrf_fuse_section_hits(
    ranked_lists: list[StrategyResult],
    k: int = DEFAULT_RRF_K,
) -> list[FusedHit]:
    """RRF over ``(document_id, line_num, score)`` tuples. The existing
    ``astrocyte.pipeline.fusion.rrf_fusion`` is keyed on a string ``id``;
    section grain is a composite ``(doc, line)`` so we specialise here.

    Hindsight uses k=60 (the original Cormack et al. RRF default); we
    keep that. ``per_strategy_rank`` is preserved so the reranker (PR2
    commit C) can inspect why a section was promoted.
    """
    accum: dict[tuple[str, int], FusedHit] = {}
    for sr in ranked_lists:
        if sr.error:
            continue
        for rank, (doc_id, line_num, _score) in enumerate(sr.hits, start=1):
            key = (doc_id, line_num)
            entry = accum.get(key)
            if entry is None:
                entry = FusedHit(
                    document_id=doc_id,
                    line_num=line_num,
                    rrf_score=0.0,
                )
                accum[key] = entry
            entry.rrf_score += 1.0 / (k + rank)
            entry.per_strategy_rank[sr.strategy] = rank
    fused = sorted(accum.values(), key=lambda h: h.rrf_score, reverse=True)
    return fused


# ── Mode dispatch ────────────────────────────────────────────────────


def select_strategies_for_mode(mode: str) -> set[str]:
    """Per-mode strategy mix. Returns a set of strategy names; the
    orchestrator only fires the named strategies (others get an empty
    StrategyResult). PR2 commit D may replace this with an LLM-driven
    weighted mix.

    Defaults: every mode runs semantic + keyword + entity (the always-
    on signals). Modes add temporal / graph_expand / speaker filters
    on top.
    """
    # Always-on baseline.
    base = {"semantic", "keyword", "entity"}
    if mode in {"temporal", "temporal-reasoning"}:
        return base | {"temporal"}
    if mode in {"multi-hop", "multi-session", "knowledge-update"}:
        return base | {"graph_expand"}
    if mode in {"single-session-assistant", "assistant-recall"}:
        # Keyword strategy is replaced with a speaker-filtered variant
        # (handled inline by the orchestrator); same set.
        return base
    return base


# ── Orchestrator ─────────────────────────────────────────────────────


import asyncio  # noqa: E402 — placed after types/helpers per module style
import logging  # noqa: E402
import time  # noqa: E402

logger = logging.getLogger("astrocyte.pipeline.section_recall")


async def section_recall(
    *,
    store: PageIndexStore,
    bank_id: str,
    question: str,
    mode: str,
    embedding_provider: LLMProvider,
    question_entities: list[str] | None = None,
    date_range: tuple[datetime, datetime] | None = None,
    semantic_seed_count: int = 20,
    rrf_k: int = DEFAULT_RRF_K,
    per_strategy_top_k: int = 20,
    wiki_enabled: bool = False,
    wiki_document_id: str | None = None,
    wiki_min_score: float = 0.55,
    wiki_top_k: int = 3,
) -> SectionRecallResult:
    """Run all selected strategies in parallel, RRF-fuse, return.

    Operates on **sections** (PageIndex tree nodes) — the M9 middle
    recall layer in ``recall.md``'s three-layer stack. Wiki recall
    sits above this; raw memory_units below.

    Args:
      store: PageIndexStore SPI handle (in-memory or postgres).
      bank_id: Scope to one bank (multi-bank later).
      question: Raw question text — passed as-is to the keyword and
        embedding strategies.
      mode: Pre-computed mode label (e.g. "multi-hop", "temporal").
        Drives which strategies fire.
      embedding_provider: LLM provider with an ``embed`` method. Only
        called when the semantic strategy is in the mix and the caller
        didn't pre-compute the question embedding.
      question_entities: Pre-extracted entities for the entity strategy.
        When None, the entity strategy is skipped (caller didn't
        prepare them — typically because PR2 commit D's question-
        annotator hasn't run).
      date_range: Pre-parsed date window for the temporal strategy.
        When None, temporal strategy is skipped.
      semantic_seed_count: Top-K for the semantic call.
      rrf_k: RRF smoothing constant.
      per_strategy_top_k: Top-K limit per strategy before fusion.

    Returns:
      ``SectionRecallResult`` with the fused list (sorted by rrf_score
      desc) plus per-strategy traces for debugging.
    """
    t0 = time.monotonic()
    selected = select_strategies_for_mode(mode)

    # Build strategy coroutines lazily so we only embed the question
    # when the semantic strategy is selected.
    async def _semantic() -> StrategyResult:
        ts = time.monotonic()
        try:
            embeds = await embedding_provider.embed([question])
            qvec = embeds[0] if embeds else []
            hits = await store.search_sections_semantic(
                bank_id, qvec, top_k=semantic_seed_count,
            )
            return StrategyResult(
                strategy="semantic", hits=hits,
                elapsed_ms=(time.monotonic() - ts) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001
            return StrategyResult(
                strategy="semantic", hits=[],
                elapsed_ms=(time.monotonic() - ts) * 1000.0,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _keyword() -> StrategyResult:
        ts = time.monotonic()
        try:
            speaker = "assistant" if mode in {"single-session-assistant", "assistant-recall"} else None
            hits = await store.search_sections_keyword(
                bank_id, question, top_k=per_strategy_top_k, speaker=speaker,
            )
            return StrategyResult(
                strategy="keyword", hits=hits,
                elapsed_ms=(time.monotonic() - ts) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001
            return StrategyResult(
                strategy="keyword", hits=[],
                elapsed_ms=(time.monotonic() - ts) * 1000.0,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _entity() -> StrategyResult:
        ts = time.monotonic()
        if not question_entities:
            return StrategyResult(strategy="entity", hits=[], elapsed_ms=0.0)
        try:
            hits = await store.search_sections_by_entities(
                bank_id, question_entities, top_k=per_strategy_top_k,
            )
            return StrategyResult(
                strategy="entity", hits=hits,
                elapsed_ms=(time.monotonic() - ts) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001
            return StrategyResult(
                strategy="entity", hits=[],
                elapsed_ms=(time.monotonic() - ts) * 1000.0,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _temporal() -> StrategyResult:
        ts = time.monotonic()
        if date_range is None:
            return StrategyResult(strategy="temporal", hits=[], elapsed_ms=0.0)
        try:
            hits = await store.search_sections_temporal(
                bank_id, date_range, top_k=per_strategy_top_k,
            )
            return StrategyResult(
                strategy="temporal", hits=hits,
                elapsed_ms=(time.monotonic() - ts) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001
            return StrategyResult(
                strategy="temporal", hits=[],
                elapsed_ms=(time.monotonic() - ts) * 1000.0,
                error=f"{type(exc).__name__}: {exc}",
            )

    # Graph-expand needs seeds — uses the union of semantic + entity
    # hits as inputs. We sequence it AFTER semantic + entity finish so
    # we have something to expand from.
    tasks: list = []
    semantic_task = asyncio.create_task(_semantic()) if "semantic" in selected else None
    keyword_task = asyncio.create_task(_keyword()) if "keyword" in selected else None
    entity_task = asyncio.create_task(_entity()) if "entity" in selected else None
    temporal_task = asyncio.create_task(_temporal()) if "temporal" in selected else None
    for t in (semantic_task, keyword_task, entity_task, temporal_task):
        if t is not None:
            tasks.append(t)

    initial_results = await asyncio.gather(*tasks)
    by_name = {r.strategy: r for r in initial_results}

    # Graph-expand: run after we have semantic + entity seeds.
    if "graph_expand" in selected:
        ts = time.monotonic()
        seeds: list[tuple[str, int]] = []
        for name in ("semantic", "entity"):
            r = by_name.get(name)
            if r:
                # Take top-5 from each as seeds — enough for 1-hop
                # bridge without exploding the join.
                seeds.extend([(d, ln) for d, ln, _ in r.hits[:5]])
        # Dedupe seeds preserving order.
        seen: set[tuple[str, int]] = set()
        unique_seeds = [s for s in seeds if not (s in seen or seen.add(s))]
        try:
            hits = await store.expand_section_links(
                unique_seeds, top_k=per_strategy_top_k,
            )
            initial_results.append(StrategyResult(
                strategy="graph_expand", hits=hits,
                elapsed_ms=(time.monotonic() - ts) * 1000.0,
            ))
        except Exception as exc:  # noqa: BLE001
            initial_results.append(StrategyResult(
                strategy="graph_expand", hits=[],
                elapsed_ms=(time.monotonic() - ts) * 1000.0,
                error=f"{type(exc).__name__}: {exc}",
            ))

    fused = _rrf_fuse_section_hits(initial_results, k=rrf_k)

    # M10.1 wiki tier — pre-aggregated observations sit ABOVE sections.
    # When enabled, we semantic-search the bank's wiki pages and surface
    # any high-confidence hit. The bench prepends these as
    # ``[OBSERVATION]`` blocks to the synth excerpts so multi-session
    # / preference questions read pre-aggregated facts instead of
    # making the LLM aggregate from raw section text.
    wiki_hits: list = []
    if wiki_enabled:
        ts = time.monotonic()
        try:
            # Reuse the embedding from the semantic strategy when present
            # so we don't pay for a second embed call per question.
            sem_result = by_name.get("semantic") if "semantic" in selected else None
            if sem_result and sem_result.hits:
                # The semantic strategy's embed already happened; refetch
                # by embedding the question again is the simple path
                # (one extra call). Cheap enough at our gate sizes.
                qvec = (await embedding_provider.embed([question]))[0]
            else:
                qvec = (await embedding_provider.embed([question]))[0]
            raw_hits = await store.search_wiki_pages_semantic(
                bank_id, qvec,
                top_k=wiki_top_k,
                document_id=wiki_document_id,
            )
            wiki_hits = [h for h in raw_hits if h.score >= wiki_min_score]
            initial_results.append(StrategyResult(
                strategy="wiki",
                hits=[(h.page_id, 0, h.score) for h in wiki_hits],
                elapsed_ms=(time.monotonic() - ts) * 1000.0,
            ))
        except Exception as exc:  # noqa: BLE001
            initial_results.append(StrategyResult(
                strategy="wiki", hits=[],
                elapsed_ms=(time.monotonic() - ts) * 1000.0,
                error=f"{type(exc).__name__}: {exc}",
            ))

    return SectionRecallResult(
        fused=fused,
        strategies=initial_results,
        mode=mode,
        elapsed_ms=(time.monotonic() - t0) * 1000.0,
        wiki_hits=wiki_hits,
    )
