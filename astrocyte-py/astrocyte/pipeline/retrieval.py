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
from datetime import datetime, timezone
from typing import TYPE_CHECKING

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
    """
    tasks: dict[str, asyncio.Task[list[ScoredItem]]] = {}

    # Always run semantic search
    tasks["semantic"] = asyncio.create_task(_semantic_search(vector_store, query_vector, bank_id, limit, filters))

    # HyDE (R1): second semantic pass with hypothetical-document embedding.
    # Runs concurrently with the standard semantic strategy; RRF fusion merges
    # both result sets.  No-op when hyde_vector is None (feature disabled or
    # generation failed upstream).
    if hyde_vector is not None:
        tasks["hyde"] = asyncio.create_task(_semantic_search(vector_store, hyde_vector, bank_id, limit, filters))

    # Graph search if store configured and entities found
    if graph_store and entity_ids:
        tasks["graph"] = asyncio.create_task(_graph_search(graph_store, entity_ids, bank_id, limit))

    # Full-text search if document store configured
    if document_store:
        tasks["keyword"] = asyncio.create_task(_keyword_search(document_store, query_text, bank_id, limit))

    # Temporal search if the vector store can enumerate. Capped scan keeps
    # cost bounded; rank by metadata[_created_at]/occurred_at recency decay.
    as_of = filters.as_of if filters is not None else None
    if enable_temporal and hasattr(vector_store, "list_vectors"):
        tasks["temporal"] = asyncio.create_task(
            _temporal_search(
                vector_store, bank_id, limit,
                scan_cap=temporal_scan_cap,
                half_life_days=temporal_half_life_days,
                as_of=as_of,
            )
        )

    # Wait for all strategies
    results: dict[str, list[ScoredItem]] = {}
    for name, task in tasks.items():
        try:
            results[name] = await task
        except Exception as exc:  # pragma: no cover — per-strategy isolation
            logger.warning("retrieval strategy %s failed: %s", name, exc)
            results[name] = []  # Strategy failure should not block others

    return results


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
            retained_at=getattr(h, "retained_at", None),
        )
        for h in hits
    ]


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
) -> list[ScoredItem]:
    """BM25 full-text search."""
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
    # Accumulate via paginated list_vectors so large banks don't blow memory.
    scanned: list[VectorItem] = []
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
            retained_at=getattr(item, "retained_at", None),
        )
        for score, item in top
    ]


def _extract_timestamp(item: VectorItem) -> datetime | None:
    """Best-effort timestamp extraction for temporal ranking.

    Precedence: ``metadata["_created_at"]`` (written by the retain path
    for MIP min_age_days enforcement) → ``occurred_at`` (when the caller
    set it explicitly). ISO strings and datetime instances both accepted;
    naive datetimes are interpreted as UTC.
    """
    metadata = item.metadata or {}
    raw = metadata.get("_created_at")
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            dt = None
    elif isinstance(raw, datetime):
        dt = raw
    else:
        dt = None

    if dt is None and item.occurred_at is not None:
        dt = item.occurred_at

    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
