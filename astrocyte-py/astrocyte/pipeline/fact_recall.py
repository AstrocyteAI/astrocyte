"""Fact-grain recall — core entry point with RRF fusion.

Unified fact-grain search that runs up to three strategies as PARALLEL
SIBLINGS and merges them via Reciprocal Rank Fusion (Hindsight-parity
architecture; see docs/_design/m18-quick-wins.md §3.3):

  - **Semantic fact search** — always runs. Cosine over fact-text
    embeddings via ``store.search_facts_semantic``.
  - **Episodic fact search** (M18a-4, gated) — when
    ``config.episodic_extract.enabled`` AND the question matches an
    episodic cue (``question_has_episodic_cue``), additionally search
    by ``EPISODIC_MARKER`` entity to surface facts tagged at retain
    time.
  - **Temporal fact search** (M18a-1 Pass B integration) — when the
    caller passes ``temporal_range=(start, end)``, additionally search
    by ``occurred_start`` overlapping the range via
    ``store.search_facts_temporal``.

Why RRF instead of append-then-rerank:
  RRF scores each candidate by ``Σ 1/(k + rank)`` across the strategies
  that surfaced it. A junk candidate that only appears at rank-1 of a
  bogus temporal hit contributes ``1/(60+1) ≈ 0.016`` to the final
  fusion score; a real answer that's rank-1 in semantic AND rank-3 in
  temporal contributes ``1/61 + 1/63 ≈ 0.032`` — twice as much.
  False-positive dateparser hits get damped automatically, and
  cross-strategy agreement gets rewarded.

  Compare to the old "append everything, let the cross-encoder sort it
  out" path: the reranker is source-blind, so a junk candidate whose
  text happens to vaguely match the query can displace a real one
  purely from rerank score noise.

Public API:
    fact_recall(*, store, bank_id, document_id, query, query_embedding,
                config, temporal_range=None,
                top_k_semantic=40, top_k_episodic=20, top_k_temporal=20,
                rrf_k=60)
        -> list[PageIndexFact]

Backward compatibility:
  Callers that don't pass ``temporal_range`` get the same 2-strategy
  semantic+(optional episodic) recall behavior. The fused result is
  ordered by RRF score (descending), which is a different ordering
  than the legacy semantic-then-episodic-append, but the downstream
  cross-encoder rerank consumes the list as an unordered candidate
  pool — so the only observable change for those callers is that
  episodic hits which co-occur with semantic hits get a slightly
  higher fusion-induced ranking, which can only help the reranker.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from astrocyte.pipeline.fusion import DEFAULT_RRF_K
from astrocyte.pipeline.intent_weights import weights_for_intent

if TYPE_CHECKING:
    from datetime import datetime

    from astrocyte.config import AstrocyteConfig
    from astrocyte.pipeline.query_intent import QueryIntent
    from astrocyte.types import PageIndexFact

_logger = logging.getLogger("astrocyte.pipeline.fact_recall")


async def fact_recall(
    *,
    store: Any,
    bank_id: str,
    document_id: str | None,
    query: str,
    query_embedding: list[float],
    config: AstrocyteConfig,
    temporal_range: tuple[datetime, datetime] | None = None,
    query_entities: list[str] | None = None,
    top_k_semantic: int = 40,
    top_k_episodic: int = 20,
    top_k_temporal: int = 20,
    top_k_link_expansion: int = 20,
    top_k_keyword: int = 20,  # M31c BM25 over fact_text
    rrf_k: int = DEFAULT_RRF_K,
    session_filter: str | None = None,
    intent: QueryIntent | None = None,  # M34-2 — intent-weighted RRF
    fact_types: list[str] | None = None,  # M34-4 — per-fact-type segmentation
    max_tokens: int | None = None,  # M35-2 — token budget cap on merged output
) -> list[PageIndexFact]:
    """Run unified fact-grain recall and return an RRF-fused ranked list.

    Always runs semantic search. Optionally runs:
      - Episodic — when ``config.episodic_extract.enabled`` AND
        ``question_has_episodic_cue(query)`` matches.
      - Temporal — when ``temporal_range`` is provided (caller
        typically gets this from ``query_analyzer.analyze_query``).
      - **Link-expansion (M27)** — when ``query_entities`` is provided
        and non-empty. For each query entity, fetches facts whose
        ``entities`` array contains that entity, ACROSS ALL SESSIONS
        in the bank (no ``document_id`` filter — that's the whole
        point of the cross-session graph traversal). Mirrors
        Hindsight's ``link_expansion_retrieval`` strategy. Lets
        questions like "who else did I discuss this with" surface
        facts from sessions the query embedding wouldn't naturally
        reach.

    Branches run in parallel via ``asyncio.gather``. Per-branch failures
    are isolated (logged + treated as empty list).

    M34-4 — when ``fact_types`` is provided, retrieval runs **per
    fact_type**: each fact_type gets its own 4-channel run (filtered
    via the M34-3 SPI param) and its own RRF-fused pool. Final result
    is the concatenation of each pool's top-``per_type_k`` (where
    ``per_type_k = ceil(final_top_n / len(fact_types))``). This
    Hindsight-parity segmentation prevents a flood in one channel
    (e.g. temporal returning experience facts) from displacing
    relevant facts of other types (e.g. preference). See
    ``docs/_design/m34-query-intent-routing.md`` for the v015i/v015j
    forensic that motivated this.

    Returns:
      Facts ranked by RRF score (highest first). Dedupe by ``fact_id``.
      The caller's downstream cross-encoder rerank picks the final
      ranking; RRF here primarily ensures that source-blind rerank
      doesn't get polluted by single-strategy junk.
    """
    # M34-4 — when fact_types is provided, segment retrieval per type.
    # Default (None) preserves the pre-M34 single-pool behaviour for BC.
    if fact_types:
        # M35-2 — per-type pool is fetched generously (~50 each); the
        # final pack_to_budget step trims the merged result by token
        # count. This gives each fact_type a fair shot at contributing
        # to the budget without a hard per-type item cap.
        per_type_pools: list[list[PageIndexFact]] = []
        for ft in fact_types:
            pool = await _run_channels_and_fuse(
                store=store, bank_id=bank_id, document_id=document_id,
                query=query, query_embedding=query_embedding, config=config,
                temporal_range=temporal_range, query_entities=query_entities,
                top_k_semantic=top_k_semantic, top_k_episodic=top_k_episodic,
                top_k_temporal=top_k_temporal,
                top_k_link_expansion=top_k_link_expansion,
                rrf_k=rrf_k, session_filter=session_filter,
                intent=intent, fact_type=ft,
            )
            per_type_pools.append(pool)
        return _merge_per_type_pools(per_type_pools, max_tokens=max_tokens)

    pool = await _run_channels_and_fuse(
        store=store, bank_id=bank_id, document_id=document_id,
        query=query, query_embedding=query_embedding, config=config,
        temporal_range=temporal_range, query_entities=query_entities,
        top_k_semantic=top_k_semantic, top_k_episodic=top_k_episodic,
        top_k_temporal=top_k_temporal,
        top_k_link_expansion=top_k_link_expansion,
        rrf_k=rrf_k, session_filter=session_filter,
        intent=intent, fact_type=None,
    )
    # M35-2 — apply token budget to the single-pool path too.
    if max_tokens is not None and max_tokens > 0:
        from astrocyte.pipeline.token_budget import pack_to_budget  # noqa: PLC0415

        pool = pack_to_budget(
            pool,
            max_tokens=max_tokens,
            text_of=lambda f: getattr(f, "text", "") or "",
        )
    return pool


async def _run_channels_and_fuse(
    *,
    store: Any,
    bank_id: str,
    document_id: str | None,
    query: str,
    query_embedding: list[float],
    config: AstrocyteConfig,
    temporal_range: tuple[datetime, datetime] | None,
    query_entities: list[str] | None,
    top_k_semantic: int,
    top_k_episodic: int,
    top_k_temporal: int,
    top_k_link_expansion: int,
    rrf_k: int,
    session_filter: str | None,
    intent: QueryIntent | None,
    fact_type: str | None,
) -> list[PageIndexFact]:
    """Run the 4 channels in parallel + fuse. Extracted from
    :func:`fact_recall` so per-fact-type segmentation can call it once
    per type. When ``fact_type`` is non-None, each SPI call filters to
    that single fact_type (M34-3)."""
    # Resolve the episodic gate cheaply (no DB call) before scheduling tasks.
    want_episodic = _is_episodic_enabled(config) and _question_has_episodic_cue(query)

    # M31 Fix 2 — session_filter applies to semantic / episodic /
    # temporal branches (which would otherwise span all sessions in the
    # document) but DELIBERATELY NOT to link-expansion: link-expansion's
    # purpose is cross-session entity traversal, so constraining it to
    # one session defeats the point. Real systems passing session_id
    # still benefit from cross-session entity matches surfacing in the
    # candidate pool — the cross-encoder rerank picks the best.
    semantic_task = _safe_call(
        "semantic",
        store.search_facts_semantic(
            bank_id, query_embedding,
            top_k=top_k_semantic, document_id=document_id,
            fact_type=fact_type,  # M34-3
            session_filter=session_filter,
        ),
    )
    tasks: list[asyncio.Task[list[PageIndexFact]]] = [
        asyncio.create_task(semantic_task, name="fact_recall.semantic"),
    ]

    episodic_idx: int | None = None
    if want_episodic:
        episodic_idx = len(tasks)
        tasks.append(
            asyncio.create_task(
                _safe_call(
                    "episodic",
                    _search_episodic(
                        store, bank_id, document_id, top_k_episodic,
                        fact_type=fact_type,
                        session_filter=session_filter,
                    ),
                ),
                name="fact_recall.episodic",
            ),
        )

    temporal_idx: int | None = None
    if temporal_range is not None:
        temporal_idx = len(tasks)
        tasks.append(
            asyncio.create_task(
                _safe_call(
                    "temporal",
                    store.search_facts_temporal(
                        bank_id, temporal_range,
                        top_k=top_k_temporal, document_id=document_id,
                        fact_type=fact_type,  # M34-3
                        session_filter=session_filter,
                    ),
                ),
                name="fact_recall.temporal",
            ),
        )

    # M27 — link-expansion: cross-session entity-graph traversal.
    # No ``document_id`` filter AND no ``session_filter`` — the whole
    # point is to surface facts from OTHER sessions that share entities
    # with the query (see M31 Fix 2 design note above).
    link_idx: int | None = None
    if query_entities:
        link_idx = len(tasks)
        tasks.append(
            asyncio.create_task(
                _safe_call(
                    "link_expansion",
                    _search_link_expansion(
                        store, bank_id, query_entities, top_k_link_expansion,
                        fact_type=fact_type,
                    ),
                ),
                name="fact_recall.link_expansion",
            ),
        )

    # M34-5 — BM25 keyword channel, intent-gated. The 5th-sibling
    # regression in M31c was caused by uniform-weight RRF flooding
    # synthesis-heavy categories. With intent weights, BM25 only
    # contributes meaningfully when the intent prefers it (FACTUAL
    # weights bm25=1.5, others 1.0 or below). When intent is None
    # (pre-M34 BC path), BM25 stays off entirely — preserves the
    # M31c-era decision until the bench wiring (M34-6) starts passing
    # intent.
    keyword_idx: int | None = None
    if intent is not None and weights_for_intent(intent).bm25 > 0.0 and hasattr(store, "search_facts_keyword"):
        keyword_idx = len(tasks)
        tasks.append(
            asyncio.create_task(
                _safe_call(
                    "keyword",
                    store.search_facts_keyword(
                        bank_id, query,
                        top_k=20,  # bound; intent weight controls effective influence
                        document_id=document_id,
                        fact_type=fact_type,
                        session_filter=session_filter,
                    ),
                ),
                name="fact_recall.keyword",
            ),
        )

    results = await asyncio.gather(*tasks)
    semantic_hits = results[0]
    episodic_hits = results[episodic_idx] if episodic_idx is not None else []
    temporal_hits = results[temporal_idx] if temporal_idx is not None else []
    link_hits = results[link_idx] if link_idx is not None else []
    keyword_hits = results[keyword_idx] if keyword_idx is not None else []

    # M34-2 — intent-weighted RRF. When ``intent`` is None we fall back
    # to equal-weight fusion (identical to pre-M34 behaviour). When
    # provided, the intent's per-channel weights bias which strategy
    # contributes most. See ``astrocyte.pipeline.intent_weights`` for
    # the calibration table and rationale.
    if intent is None:
        return _rrf_fuse_fact_hits(
            [semantic_hits, episodic_hits, temporal_hits, link_hits],
            k=rrf_k,
        )

    w = weights_for_intent(intent)
    return _rrf_fuse_fact_hits_weighted(
        [
            (semantic_hits, w.semantic),
            (episodic_hits, w.episodic),
            (temporal_hits, w.temporal),
            (link_hits, w.link_expansion),
            (keyword_hits, w.bm25),
        ],
        k=rrf_k,
    )


def _merge_per_type_pools(
    pools: list[list[PageIndexFact]],
    *,
    max_tokens: int | None,
) -> list[PageIndexFact]:
    """M34-4 + M35-2 — round-robin interleave per-type pools, dedupe by
    fact_id, then token-budget cap.

    Within-pool order is preserved (per-type RRF rank). Cross-pool order
    is round-robin so we don't bias toward whichever fact_type happens
    to be first in ``fact_types`` — round-robin gives every type's top
    hit a slot before any type's second hit.

    M35-2: the final trim is by ``max_tokens`` (token budget) rather
    than item count. When ``max_tokens`` is None, all deduped items
    are returned (legacy callers + tests can opt out).
    """
    # Round-robin interleave so each type contributes alternately.
    interleaved: list[PageIndexFact] = []
    max_len = max((len(p) for p in pools), default=0)
    for i in range(max_len):
        for pool in pools:
            if i < len(pool):
                interleaved.append(pool[i])

    # Dedupe by fact_id preserving first-seen order.
    seen: set[str] = set()
    out: list[PageIndexFact] = []
    for hit in interleaved:
        fid = getattr(hit, "fact_id", None)
        if fid is None or fid in seen:
            continue
        seen.add(fid)
        out.append(hit)

    if max_tokens is not None and max_tokens > 0:
        from astrocyte.pipeline.token_budget import pack_to_budget  # noqa: PLC0415

        out = pack_to_budget(
            out,
            max_tokens=max_tokens,
            text_of=lambda f: getattr(f, "text", "") or "",
        )
    return out


# ─────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────


async def _safe_call(
    branch_name: str,
    coro: Any,
) -> list[PageIndexFact]:
    """Await ``coro`` and treat exceptions as empty result.

    Per-branch failure isolation is critical for the recall path —
    a temporary DB index issue on (say) the temporal SPI must NOT
    take down semantic recall too.
    """
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001
        _logger.warning("fact_recall: %s branch failed: %s", branch_name, exc)
        return []


async def _search_episodic(
    store: Any,
    bank_id: str,
    document_id: str | None,
    top_k: int,
    *,
    fact_type: str | None = None,  # M34-3 — per-fact-type segmentation
    session_filter: str | None = None,
) -> list[PageIndexFact]:
    """Lazy import of EPISODIC_MARKER + search_facts_by_entity call.

    M31 Fix 2: episodic facts are session-anchored (a section emits at
    most one EPISODIC_MARKER fact for its session), so session_filter
    naturally scopes the result to the matching session's episodic facts.
    """
    from astrocyte.pipeline.episodic_extract import EPISODIC_MARKER  # noqa: PLC0415

    return await store.search_facts_by_entity(
        bank_id, EPISODIC_MARKER,
        top_k=top_k, document_id=document_id,
        fact_type=fact_type,
        session_filter=session_filter,
    )


async def _search_link_expansion(
    store: Any,
    bank_id: str,
    query_entities: list[str],
    top_k_per_entity: int,
    *,
    fact_type: str | None = None,  # M34-3 — per-fact-type segmentation
) -> list[PageIndexFact]:
    """M27 — cross-session entity-graph traversal.

    For each query entity, fetch facts whose ``entities`` array
    contains it (case-insensitive via ``search_facts_by_entity``).
    Crucially passes ``document_id=None`` so the search spans ALL
    sessions in the bank — that's the "cross-session" part. Single-
    session matches will also show up via the semantic strategy; this
    branch's value-add is the multi-session hits.

    Per-entity results are interleaved with dedupe-by-fact-id. The
    cap is per entity to bound cost when the query has many entities;
    the RRF fusion downstream will pick the most-prominent facts
    across the combined pool.

    Hindsight reference: ``hindsight_api/engine/search/link_expansion_retrieval.py``.
    """
    if not query_entities:
        return []

    # Run per-entity searches in parallel, dedupe by fact_id, preserve
    # first-seen ordering (later RRF reranks anyway).
    tasks = [
        asyncio.create_task(
            _safe_call(
                f"link_expansion[{ent}]",
                store.search_facts_by_entity(
                    bank_id, ent, top_k=top_k_per_entity, document_id=None,
                    fact_type=fact_type,  # M34-3
                ),
            ),
            name=f"fact_recall.link_expansion.{ent[:32]}",
        )
        for ent in query_entities
    ]
    per_entity_results = await asyncio.gather(*tasks)

    seen: set[str] = set()
    merged: list[PageIndexFact] = []
    # Interleave: take 1 from each entity's list per round (round-robin)
    # to give every query entity fair representation, not just the
    # first one's top-K.
    max_len = max((len(r) for r in per_entity_results), default=0)
    for i in range(max_len):
        for ent_hits in per_entity_results:
            if i >= len(ent_hits):
                continue
            fact = ent_hits[i]
            fid = getattr(fact, "fact_id", None) or getattr(fact, "id", None)
            if fid is None or fid in seen:
                continue
            seen.add(fid)
            merged.append(fact)
    return merged


def _rrf_fuse_fact_hits(
    ranked_lists: list[list[PageIndexFact]],
    *,
    k: int = DEFAULT_RRF_K,
) -> list[PageIndexFact]:
    """Reciprocal Rank Fusion over fact-hit lists, dedupe by ``fact_id``.

    Each rank-r appearance contributes ``1.0 / (k + r + 1)`` to the
    fused score (r is 0-indexed). Hits without a ``fact_id`` are
    dropped (we cannot dedupe them safely).

    Returns facts ordered by descending fused score. When a fact
    appears in multiple ranked lists, its highest-scoring instance is
    kept as the representative.
    """
    fused_score: dict[str, float] = {}
    representative: dict[str, PageIndexFact] = {}

    for ranked_list in ranked_lists:
        if not ranked_list:
            continue
        for rank, hit in enumerate(ranked_list):
            fid = getattr(hit, "fact_id", None)
            if fid is None:
                continue
            fused_score[fid] = fused_score.get(fid, 0.0) + 1.0 / (k + rank + 1)
            # Keep the first-seen hit as the representative; ranking is
            # determined by the fused score below, so the per-instance
            # rank within its source list doesn't matter for ordering.
            if fid not in representative:
                representative[fid] = hit

    sorted_ids = sorted(
        fused_score.keys(),
        key=lambda fid: fused_score[fid],
        reverse=True,
    )
    return [representative[fid] for fid in sorted_ids]


def _rrf_fuse_fact_hits_weighted(
    ranked_lists_with_weights: list[tuple[list[PageIndexFact], float]],
    *,
    k: int = DEFAULT_RRF_K,
) -> list[PageIndexFact]:
    """M34-2 — weighted RRF over fact-hit lists.

    Each rank-r appearance contributes ``weight / (k + r + 1)`` to the
    fused score. A list with weight 0.0 is skipped entirely (no items
    contribute, no items added to the candidate pool). Negative weights
    are a caller bug and raise ``ValueError``.

    Mirrors the contract of
    :func:`astrocyte.pipeline.fusion.weighted_rrf_fusion` but operates
    on :class:`PageIndexFact` instances directly so we don't pay the
    ScoredItem conversion cost. The two functions stay in sync — if
    you change one, change the other.

    When all weights are 1.0 this is mathematically identical to
    :func:`_rrf_fuse_fact_hits`.
    """
    fused_score: dict[str, float] = {}
    representative: dict[str, PageIndexFact] = {}

    for ranked_list, weight in ranked_lists_with_weights:
        if weight < 0.0:
            raise ValueError(
                f"RRF weight must be >= 0.0; got {weight!r}. Pass weight=0.0 "
                "to mute a channel.",
            )
        if weight == 0.0:
            continue
        if not ranked_list:
            continue
        for rank, hit in enumerate(ranked_list):
            fid = getattr(hit, "fact_id", None)
            if fid is None:
                continue
            fused_score[fid] = fused_score.get(fid, 0.0) + weight / (k + rank + 1)
            if fid not in representative:
                representative[fid] = hit

    sorted_ids = sorted(
        fused_score.keys(),
        key=lambda fid: fused_score[fid],
        reverse=True,
    )
    return [representative[fid] for fid in sorted_ids]


def _is_episodic_enabled(config: AstrocyteConfig) -> bool:
    """Gate: ``config.episodic_extract.enabled`` must be True."""
    sub = getattr(config, "episodic_extract", None)
    if sub is None:
        return False
    return bool(getattr(sub, "enabled", False))


def _question_has_episodic_cue(query: str) -> bool:
    """Gate: question must match a known episodic cue regex.

    Lazy-imported so module load doesn't pay the cost when episodic
    extraction is disabled (typical M17 baseline). On ImportError
    (episodic_extract module unavailable for some reason), return
    False — no cue means no episodic branch.
    """
    try:
        from astrocyte.pipeline.episodic_extract import (  # noqa: PLC0415
            question_has_episodic_cue,
        )
    except ImportError:
        return False
    return bool(question_has_episodic_cue(query))
