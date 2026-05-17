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

if TYPE_CHECKING:
    from datetime import datetime

    from astrocyte.config import AstrocyteConfig
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
    top_k_semantic: int = 40,
    top_k_episodic: int = 20,
    top_k_temporal: int = 20,
    rrf_k: int = DEFAULT_RRF_K,
) -> list[PageIndexFact]:
    """Run unified fact-grain recall and return an RRF-fused ranked list.

    Always runs semantic search. Optionally runs:
      - Episodic — when ``config.episodic_extract.enabled`` AND
        ``question_has_episodic_cue(query)`` matches.
      - Temporal — when ``temporal_range`` is provided (caller
        typically gets this from ``query_analyzer.analyze_query``).

    Branches run in parallel via ``asyncio.gather``. Per-branch failures
    are isolated (logged + treated as empty list).

    Returns:
      Facts ranked by RRF score (highest first). Dedupe by ``fact_id``.
      The caller's downstream cross-encoder rerank picks the final
      ranking; RRF here primarily ensures that source-blind rerank
      doesn't get polluted by single-strategy junk.
    """
    # Resolve the episodic gate cheaply (no DB call) before scheduling tasks.
    want_episodic = _is_episodic_enabled(config) and _question_has_episodic_cue(query)

    semantic_task = _safe_call(
        "semantic",
        store.search_facts_semantic(
            bank_id, query_embedding,
            top_k=top_k_semantic, document_id=document_id,
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
                    _search_episodic(store, bank_id, document_id, top_k_episodic),
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
                    ),
                ),
                name="fact_recall.temporal",
            ),
        )

    results = await asyncio.gather(*tasks)
    semantic_hits = results[0]
    episodic_hits = results[episodic_idx] if episodic_idx is not None else []
    temporal_hits = results[temporal_idx] if temporal_idx is not None else []

    return _rrf_fuse_fact_hits(
        [semantic_hits, episodic_hits, temporal_hits],
        k=rrf_k,
    )


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
) -> list[PageIndexFact]:
    """Lazy import of EPISODIC_MARKER + search_facts_by_entity call."""
    from astrocyte.pipeline.episodic_extract import EPISODIC_MARKER  # noqa: PLC0415

    return await store.search_facts_by_entity(
        bank_id, EPISODIC_MARKER,
        top_k=top_k, document_id=document_id,
    )


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
