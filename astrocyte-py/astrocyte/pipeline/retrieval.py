"""Parallel multi-strategy retrieval — runs concurrent searches across stores.

Async (I/O-bound). See docs/_design/built-in-pipeline.md section 3.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from astrocyte.pipeline.fusion import ScoredItem

if TYPE_CHECKING:
    from astrocyte.provider import DocumentStore, GraphStore, VectorStore
    from astrocyte.types import VectorFilters


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
) -> dict[str, list[ScoredItem]]:
    """Run parallel retrieval across all configured stores.

    Returns a dict of {strategy_name: list[ScoredItem]}.
    Strategies run concurrently via asyncio.gather.
    """
    tasks: dict[str, asyncio.Task[list[ScoredItem]]] = {}

    # Always run semantic search
    tasks["semantic"] = asyncio.create_task(_semantic_search(vector_store, query_vector, bank_id, limit, filters))

    # Graph search if store configured and entities found
    if graph_store and entity_ids:
        tasks["graph"] = asyncio.create_task(_graph_search(graph_store, entity_ids, bank_id, limit))

    # Full-text search if document store configured
    if document_store:
        tasks["keyword"] = asyncio.create_task(_keyword_search(document_store, query_text, bank_id, limit))

    # Wait for all strategies
    results: dict[str, list[ScoredItem]] = {}
    for name, task in tasks.items():
        try:
            results[name] = await task
        except Exception:
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
