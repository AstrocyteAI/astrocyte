"""Memory consolidation — dedup and archive.

Async (coordinates I/O operations). See docs/_design/built-in-pipeline.md section 5.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from astrocyte.policy.signal_quality import cosine_similarity

if TYPE_CHECKING:
    from astrocyte.provider import VectorStore

logger = logging.getLogger("astrocyte.pipeline")


@dataclass
class ConsolidationResult:
    duplicates_removed: int
    total_scanned: int


async def run_consolidation(
    vector_store: VectorStore,
    bank_id: str,
    similarity_threshold: float = 0.95,
    batch_size: int = 100,
) -> ConsolidationResult:
    """Run basic dedup consolidation on a bank.

    Paginates through all vectors via ``list_vectors()``, compares embeddings
    pairwise, and deletes near-duplicates (keeping the first occurrence).
    """
    if not hasattr(vector_store, "list_vectors"):
        logger.warning("VectorStore does not support list_vectors; skipping consolidation")
        return ConsolidationResult(duplicates_removed=0, total_scanned=0)

    duplicates_removed = 0
    total_scanned = 0
    seen_embeddings: list[tuple[str, list[float]]] = []
    to_delete: list[str] = []

    offset = 0
    while True:
        batch = await vector_store.list_vectors(bank_id, offset=offset, limit=batch_size)
        if not batch:
            break

        for item in batch:
            total_scanned += 1

            # Compare against seen embeddings
            is_dup = False
            for _, seen_vec in seen_embeddings:
                try:
                    sim = cosine_similarity(item.vector, seen_vec)
                except ValueError:
                    continue
                if sim >= similarity_threshold:
                    to_delete.append(item.id)
                    is_dup = True
                    break

            if not is_dup:
                seen_embeddings.append((item.id, item.vector))

        offset += len(batch)
        if len(batch) < batch_size:
            break

        # Safety: prevent runaway scans
        if offset > 100000:
            logger.warning("Consolidation scan capped at 100k vectors for bank %s", bank_id)
            break

    # Delete duplicates in batches
    if to_delete:
        for i in range(0, len(to_delete), batch_size):
            chunk = to_delete[i : i + batch_size]
            deleted = await vector_store.delete(chunk, bank_id)
            duplicates_removed += deleted

    return ConsolidationResult(
        duplicates_removed=duplicates_removed,
        total_scanned=total_scanned,
    )
