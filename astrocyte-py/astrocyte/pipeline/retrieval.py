"""Parallel multi-strategy retrieval — runs concurrent searches across stores.

Async (I/O-bound). See docs/_design/built-in-pipeline.md section 3.

Strategies fused via RRF:

- ``semantic`` — dense vector similarity (always runs).
- ``keyword`` — BM25 full-text (runs when ``document_store`` is configured).
- ``graph`` — entity-graph neighbor traversal (runs when ``graph_store`` is
  configured AND query entities are resolved).
- ``temporal`` — recency-ranked list of bank vectors (runs when the store
  exposes ``list_vectors`` AND the strategy is enabled via
  ``enable_temporal=True``). Rescues recently-retained memories that
  lose the semantic cutoff to older near-matches. Inspired by Hindsight's
  4-way parallel retrieval (see
  ``docs/_design/platform-positioning.md`` §Mystique).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from astrocyte.pipeline.fusion import ScoredItem

if TYPE_CHECKING:
    from astrocyte.provider import DocumentStore, GraphStore, VectorStore
    from astrocyte.types import VectorFilters, VectorItem

logger = logging.getLogger("astrocyte.retrieval")

#: Cap on how many vectors the temporal strategy will scan per recall.
#: For large banks we can't enumerate everything on every query — 500 is a
#: reasonable default that still surfaces recent writes without crushing
#: the store. Operators can lower this via ``max_temporal_scan`` in the
#: orchestrator for hot paths.
DEFAULT_TEMPORAL_SCAN_CAP = 500

#: Half-life for the temporal score's exponential decay. A memory this many
#: days old scores 0.5; older memories fall off; fresher memories climb
#: toward 1.0. Tuned to "a week feels recent, a month feels old" for
#: conversational memory workloads. Configurable per orchestrator.
DEFAULT_TEMPORAL_HALF_LIFE_DAYS = 7.0


async def parallel_retrieve(
    query_vector: list[float],
    query_text: str,
    bank_id: str,
    vector_store: VectorStore,
    graph_store: GraphStore | None = None,
    document_store: DocumentStore | None = None,
    entity_ids: list[str] | None = None,
    limit: int = 30,
    filters: VectorFilters | None = None,
    *,
    enable_temporal: bool = True,
    temporal_scan_cap: int = DEFAULT_TEMPORAL_SCAN_CAP,
    temporal_half_life_days: float = DEFAULT_TEMPORAL_HALF_LIFE_DAYS,
    hyde_vector: list[float] | None = None,
    strategy_timings_ms: dict[str, float] | None = None,
    strategy_candidate_counts: dict[str, int] | None = None,
    use_bm25_idf: bool = False,
) -> dict[str, list[ScoredItem]]:
    """Run parallel retrieval across all configured stores.

    Returns a dict of ``{strategy_name: list[ScoredItem]}``. Strategies run
    concurrently via ``asyncio.gather``; a failure in one strategy never
    blocks the others.

    Args:
        enable_temporal: Gate the temporal strategy. Default on. Turn off
            for workloads where recency is not a signal (static document
            corpora). Requires ``vector_store.list_vectors``.
        temporal_scan_cap: Cap on vectors scanned per recall for temporal
            ranking. Guards against O(bank) cost on large stores.
        temporal_half_life_days: Exponential decay half-life for temporal
            score. Tune shorter (e.g. 1.0) for fast-moving chat workloads,
            longer (e.g. 30.0) for slower knowledge bases.
        hyde_vector: Optional pre-computed HyDE embedding (hypothetical
            document embedding).  When provided, an additional ``"hyde"``
            strategy runs semantic search with this vector and its results
            are fused via RRF alongside the standard ``"semantic"`` strategy.
            Generate with :func:`astrocyte.pipeline.hyde.generate_hyde_vector`.
        use_bm25_idf: When ``True`` AND the document store advertises
            ``search_fulltext_bm25`` (PostgresStore via migration 013),
            route the keyword strategy through the BM25-with-IDF
            materialized-view path instead of the classic ``ts_rank_cd``
            path. Skips the hybrid CTE (which uses ts_rank_cd) — keyword
            and semantic run as separate strategies. Default ``False``.
    """
    tasks: dict[str, asyncio.Task[tuple[list[ScoredItem], float]]] = {}
    hybrid_task: asyncio.Task[tuple[dict[str, list[ScoredItem]], float]] | None = None

    hybrid_search = getattr(vector_store, "search_hybrid_semantic_bm25", None)
    use_hybrid_search = (
        callable(hybrid_search)
        and document_store is vector_store
        and query_text.strip()
        and hyde_vector is None
        # BM25-IDF requires its own keyword path (the materialized-view
        # query). When enabled, skip the hybrid CTE so keyword goes
        # through ``_keyword_search`` → ``search_fulltext_bm25``.
        and not use_bm25_idf
    )

    # PostgresStore can answer semantic and keyword retrieval in one SQL round trip.
    # Other adapters keep the portable per-strategy path.
    if use_hybrid_search:
        hybrid_task = asyncio.create_task(
            _timed_hybrid_semantic_keyword_search(
                vector_store, query_vector, query_text, bank_id, limit, filters,
            )
        )
    else:
        tasks["semantic"] = asyncio.create_task(
            _timed(_semantic_search(vector_store, query_vector, bank_id, limit, filters))
        )

    # HyDE (R1): second semantic pass with hypothetical-document embedding.
    # Runs concurrently with the standard semantic strategy; RRF fusion merges
    # both result sets.  No-op when hyde_vector is None (feature disabled or
    # generation failed upstream).
    if hyde_vector is not None:
        tasks["hyde"] = asyncio.create_task(
            _timed(_semantic_search(vector_store, hyde_vector, bank_id, limit, filters))
        )

    # Graph search if store configured and entities found
    if graph_store and entity_ids:
        tasks["graph"] = asyncio.create_task(_timed(_graph_search(graph_store, entity_ids, bank_id, limit)))

    # Full-text search if document store configured
    if document_store and not use_hybrid_search:
        tasks["keyword"] = asyncio.create_task(
            _timed(_keyword_search(document_store, query_text, bank_id, limit, use_bm25_idf=use_bm25_idf))
        )

    # Temporal search if the vector store can enumerate. Capped scan keeps
    # cost bounded; rank by metadata[_created_at]/occurred_at recency decay.
    as_of = filters.as_of if filters is not None else None
    if enable_temporal and hasattr(vector_store, "list_vectors"):
        tasks["temporal"] = asyncio.create_task(
            _timed(_temporal_search(
                vector_store, bank_id, limit,
                scan_cap=temporal_scan_cap,
                half_life_days=temporal_half_life_days,
                as_of=as_of,
                filters=filters,
            ))
        )

    # Wait for all strategies
    results: dict[str, list[ScoredItem]] = {}
    if hybrid_task is not None:
        try:
            hybrid_results, elapsed_ms = await hybrid_task
            for name, items in hybrid_results.items():
                results[name] = items
                if strategy_timings_ms is not None:
                    strategy_timings_ms[name] = elapsed_ms
                if strategy_candidate_counts is not None:
                    strategy_candidate_counts[name] = len(items)
        except Exception as exc:
            # Hybrid CTE failed (transient pool error, deadlock, lock-wait,
            # OOM, etc.). DON'T clobber semantic + keyword to [] — that
            # turns one transient DB hiccup into a recall failure for the
            # entire question. Fall back to running the same two strategies
            # as separate per-store calls (the portable path other adapters
            # use). Each fallback is isolated, so if e.g. semantic succeeds
            # but keyword fails, semantic still flows through.
            logger.warning(
                "retrieval strategy hybrid_semantic_bm25 failed (%s); "
                "falling back to per-strategy semantic + keyword",
                exc,
            )
            sem_task = asyncio.create_task(
                _timed(_semantic_search(vector_store, query_vector, bank_id, limit, filters))
            )
            kw_task = asyncio.create_task(
                _timed(_keyword_search(document_store, query_text, bank_id, limit))
            )
            for name, fallback_task in (("semantic", sem_task), ("keyword", kw_task)):
                try:
                    items, fallback_ms = await fallback_task
                    results[name] = items
                    if strategy_timings_ms is not None:
                        strategy_timings_ms[name] = fallback_ms
                    if strategy_candidate_counts is not None:
                        strategy_candidate_counts[name] = len(items)
                except Exception as inner_exc:
                    logger.warning(
                        "fallback strategy %s also failed: %s", name, inner_exc,
                    )
                    results[name] = []
                    if strategy_timings_ms is not None:
                        strategy_timings_ms[name] = 0.0
                    if strategy_candidate_counts is not None:
                        strategy_candidate_counts[name] = 0

    for name, task in tasks.items():
        try:
            items, elapsed_ms = await task
            results[name] = items
            if strategy_timings_ms is not None:
                strategy_timings_ms[name] = elapsed_ms
            if strategy_candidate_counts is not None:
                strategy_candidate_counts[name] = len(items)
        except Exception as exc:  # pragma: no cover — per-strategy isolation
            logger.warning("retrieval strategy %s failed: %s", name, exc)
            results[name] = []  # Strategy failure should not block others
            if strategy_timings_ms is not None:
                strategy_timings_ms[name] = 0.0
            if strategy_candidate_counts is not None:
                strategy_candidate_counts[name] = 0

    return results


async def _timed(coro) -> tuple[list[ScoredItem], float]:
    start = time.perf_counter()
    result = await coro
    elapsed_ms = (time.perf_counter() - start) * 1000
    return result, elapsed_ms


async def _semantic_search(
    vector_store: VectorStore,
    query_vector: list[float],
    bank_id: str,
    limit: int,
    filters: VectorFilters | None,
) -> list[ScoredItem]:
    """Vector similarity search."""
    hits = await vector_store.search_similar(query_vector, bank_id, limit=limit, filters=filters)
    return [
        ScoredItem(
            id=h.id,
            text=h.text,
            score=h.score,
            fact_type=h.fact_type,
            metadata=h.metadata,
            tags=h.tags,
            occurred_at=h.occurred_at,
            retained_at=getattr(h, "retained_at", None),
        )
        for h in hits
    ]


async def _timed_hybrid_semantic_keyword_search(
    vector_store: VectorStore,
    query_vector: list[float],
    query_text: str,
    bank_id: str,
    limit: int,
    filters: VectorFilters | None,
) -> tuple[dict[str, list[ScoredItem]], float]:
    start = time.perf_counter()
    raw = await vector_store.search_hybrid_semantic_bm25(
        query_vector,
        query_text,
        bank_id,
        limit=limit,
        filters=filters,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    return _hybrid_hits_to_scored_items(raw), elapsed_ms


def _hybrid_hits_to_scored_items(raw: dict[str, list[Any]]) -> dict[str, list[ScoredItem]]:
    results: dict[str, list[ScoredItem]] = {"semantic": [], "keyword": []}
    for hit in raw.get("semantic", []):
        results["semantic"].append(
            ScoredItem(
                id=hit.id,
                text=hit.text,
                score=hit.score,
                fact_type=hit.fact_type,
                metadata=hit.metadata,
                tags=hit.tags,
                occurred_at=hit.occurred_at,
                retained_at=getattr(hit, "retained_at", None),
            )
        )
    for hit in raw.get("keyword", []):
        results["keyword"].append(
            ScoredItem(
                id=hit.document_id,
                text=hit.text,
                score=hit.score,
                metadata=hit.metadata,
            )
        )
    return results


async def _graph_search(
    graph_store: GraphStore,
    entity_ids: list[str],
    bank_id: str,
    limit: int,
) -> list[ScoredItem]:
    """Graph neighbor traversal."""
    hits = await graph_store.query_neighbors(entity_ids, bank_id, max_depth=2, limit=limit)
    return [
        ScoredItem(
            id=h.memory_id,
            text=h.text,
            score=h.score,
            fact_type=None,
        )
        for h in hits
    ]


async def _keyword_search(
    document_store: DocumentStore,
    query_text: str,
    bank_id: str,
    limit: int,
    *,
    use_bm25_idf: bool = False,
) -> list[ScoredItem]:
    """Full-text search.

    Routes through :meth:`PostgresStore.search_fulltext_bm25` (proper BM25
    with corpus IDF + length normalisation) when ``use_bm25_idf=True`` AND
    the store advertises that method; otherwise falls through to the
    classic :meth:`DocumentStore.search_fulltext` (``ts_rank_cd``).

    Stores that don't expose ``search_fulltext_bm25`` (in_memory,
    elasticsearch adapter, etc.) silently use the classic path even when
    the flag is on — the flag is "use BM25-IDF if available," not "fail
    if unavailable."
    """
    bm25_method = getattr(document_store, "search_fulltext_bm25", None)
    if use_bm25_idf and callable(bm25_method):
        hits = await bm25_method(query_text, bank_id, limit=limit)
    else:
        hits = await document_store.search_fulltext(query_text, bank_id, limit=limit)
    return [
        ScoredItem(
            id=h.document_id,
            text=h.text,
            score=h.score,
            metadata=h.metadata,
        )
        for h in hits
    ]


async def _temporal_search(
    vector_store: VectorStore,
    bank_id: str,
    limit: int,
    *,
    scan_cap: int,
    half_life_days: float,
    as_of: datetime | None = None,
    filters: VectorFilters | None = None,
) -> list[ScoredItem]:
    """Recency-ranked strategy.

    Enumerates up to ``scan_cap`` vectors from ``bank_id`` via
    ``list_vectors``, ranks them by recency decay over
    ``metadata["_created_at"]`` (falling back to ``occurred_at`` when
    present), and returns the top ``limit`` as :class:`ScoredItem`.

    The decay is exponential with half-life ``half_life_days``:
    ``score = 2 ** (-age_days / half_life_days)``. A memory exactly
    ``half_life_days`` old scores 0.5; a fresh memory approaches 1.0.

    Vectors without a usable timestamp are skipped (not ranked at the
    bottom — they simply don't contribute, because "we don't know when"
    is a different signal than "we know it's old"). When every candidate
    lacks timestamps, the result is an empty list and RRF ignores the
    strategy entirely.
    """
    recent_vectors = getattr(vector_store, "list_recent_vectors", None)
    if callable(recent_vectors):
        scanned = await recent_vectors(bank_id, limit=scan_cap, filters=filters)
    else:
        # Accumulate via paginated list_vectors so large banks don't blow memory.
        scanned = []
        offset = 0
        batch = min(200, scan_cap)
        while len(scanned) < scan_cap:
            page = await vector_store.list_vectors(bank_id, offset=offset, limit=batch)
            if not page:
                break
            scanned.extend(page)
            if len(page) < batch:
                break  # last page
            offset += batch

    if not scanned:
        return []

    now = datetime.now(timezone.utc)
    scored: list[tuple[float, VectorItem]] = []
    for item in scanned:
        # M9: time-travel filter — skip items retained after as_of
        if as_of is not None and item.retained_at is not None and item.retained_at > as_of:
            continue
        timestamp = _extract_timestamp(item)
        if timestamp is None:
            continue
        age_days = max((now - timestamp).total_seconds() / 86400.0, 0.0)
        # Exponential decay with configured half-life.
        score = math.pow(2.0, -age_days / half_life_days) if half_life_days > 0 else 1.0
        scored.append((score, item))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = scored[:limit]

    return [
        ScoredItem(
            id=item.id,
            text=item.text,
            score=score,
            fact_type=item.fact_type,
            metadata=item.metadata,
            tags=item.tags,
            memory_layer=item.memory_layer,
            occurred_at=item.occurred_at,
            retained_at=getattr(item, "retained_at", None),
        )
        for score, item in top
    ]


def _extract_timestamp(item: VectorItem) -> datetime | None:
    """Best-effort timestamp extraction for temporal ranking.

    Precedence: ``occurred_at`` (event/session time when the caller set it)
    → ``metadata["_created_at"]`` (retain time used for lifecycle/TTL).
    ISO strings and datetime instances are both accepted; naive datetimes are
    interpreted as UTC.
    """
    metadata = item.metadata or {}
    dt = item.occurred_at
    if dt is None:
        raw = metadata.get("_created_at")
        if isinstance(raw, str):
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError:
                dt = None
        elif isinstance(raw, datetime):
            dt = raw

    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
