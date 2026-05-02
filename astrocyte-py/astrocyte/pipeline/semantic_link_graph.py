"""Precomputed semantic-kNN graph (Hindsight parity, C3a).

Hindsight's link-expansion retrieval relies on a precomputed semantic
kNN graph: at retain time, each new memory is linked to its top-K
most-similar existing memories with similarity above a threshold
(default ``0.7``). At recall time, those edges become a parallel
expansion signal alongside entity-overlap and causal links.

The semantic-kNN edges are essentially a static "what's already in the
bank that's nearby in embedding space" — much cheaper to maintain than
recomputing kNN at every recall.

This module provides :func:`compute_semantic_links` which takes a
freshly-embedded batch of chunks (with their assigned memory_ids), runs
``search_similar`` against the existing bank for each, and produces
:class:`MemoryLink` records with ``link_type="semantic"``. The
orchestrator persists them via ``GraphStore.store_memory_links``.

Notes:

- Per-chunk asyncio.gather: K queries are independent; gather lets the
  bank-side concurrency soak up the cost.
- Self-exclusion: each chunk's own memory_id is filtered out of its
  own kNN result set (a chunk would otherwise link to itself with
  similarity 1.0).
- Same-batch exclusion: memories created in the same retain call are
  also filtered (we don't want chunk_2 to link to chunk_3 just because
  they shared the same source paragraph — that's already captured by
  the causal_by signal when applicable).
- Threshold: configurable, default ``0.7`` matches Hindsight.
- top-K: configurable, default ``5`` matches Hindsight.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from astrocyte.types import MemoryLink

_logger = logging.getLogger("astrocyte.semantic_link_graph")


async def compute_semantic_links(
    *,
    bank_id: str,
    new_memory_ids: list[str],
    new_embeddings: list[list[float]],
    vector_store,
    top_k: int = 5,
    similarity_threshold: float = 0.7,
) -> list[MemoryLink]:
    """Build the semantic-kNN edges for a batch of new memories.

    For each ``(memory_id, embedding)``, run a similarity search against
    ``bank_id`` and emit one :class:`MemoryLink` per hit above
    ``similarity_threshold``. The edges are directional but the
    *semantic* link is symmetric semantically — the link-expansion
    retrieval queries both directions at recall time.

    Args:
        bank_id: Target bank; the search runs scoped to this bank.
        new_memory_ids: IDs of the freshly-stored memories. Must align
            with ``new_embeddings`` index-for-index.
        new_embeddings: Embeddings for each new memory.
        vector_store: Provider implementing ``search_similar``.
        top_k: Maximum number of nearest neighbors per new memory.
        similarity_threshold: Minimum cosine similarity to keep an edge.

    Returns:
        :class:`MemoryLink` objects with ``link_type="semantic"`` and
        ``weight=similarity``. Empty list when no neighbors qualify.
    """
    if not new_memory_ids or len(new_memory_ids) != len(new_embeddings):
        return []

    same_batch = set(new_memory_ids)

    async def _search_one(idx: int) -> list[MemoryLink]:
        embedding = new_embeddings[idx]
        if not embedding:
            return []
        try:
            # Fetch a few extra so post-filtering for self + same-batch
            # exclusions still leaves us close to top_k.
            hits = await vector_store.search_similar(
                embedding, bank_id, limit=top_k + len(same_batch) + 2,
            )
        except Exception as exc:
            _logger.warning(
                "semantic_link_graph: search_similar failed for %r (%s)",
                new_memory_ids[idx], exc,
            )
            return []

        out: list[MemoryLink] = []
        now = datetime.now(timezone.utc)
        for hit in hits:
            if hit.id in same_batch:
                continue
            if hit.score < similarity_threshold:
                # search_similar returns hits sorted descending; once we
                # drop below threshold, the rest will too.
                break
            out.append(
                MemoryLink(
                    source_memory_id=new_memory_ids[idx],
                    target_memory_id=hit.id,
                    link_type="semantic",
                    evidence="",
                    confidence=1.0,
                    weight=float(hit.score),
                    created_at=now,
                    metadata={"source": "semantic_link_graph"},
                )
            )
            if len(out) >= top_k:
                break
        return out

    per_chunk = await asyncio.gather(
        *[_search_one(i) for i in range(len(new_memory_ids))]
    )
    return [link for chunk_links in per_chunk for link in chunk_links]
