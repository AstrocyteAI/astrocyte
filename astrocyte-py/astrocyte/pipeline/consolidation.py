"""Memory consolidation — dedup, stale archival, and entity cleanup.

Async (coordinates I/O operations). See docs/_design/built-in-pipeline.md section 5.

Tier 1 consolidation supports:
- Dedup: remove near-duplicate embeddings (cosine similarity)
- Stale archival: identify memories never recalled within a time window
- Entity cleanup: remove orphaned entities from the graph store
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from astrocyte.policy.signal_quality import cosine_similarity

if TYPE_CHECKING:
    from astrocyte.provider import GraphStore, VectorStore

logger = logging.getLogger("astrocyte.pipeline")


@dataclass
class ConsolidationResult:
    duplicates_removed: int
    total_scanned: int
    stale_archived: int = 0
    orphaned_entities_removed: int = 0


async def run_consolidation(
    vector_store: VectorStore,
    bank_id: str,
    similarity_threshold: float = 0.95,
    batch_size: int = 100,
    *,
    archive_unretrieved_after_days: int | None = None,
    graph_store: GraphStore | None = None,
) -> ConsolidationResult:
    """Run Tier 1 consolidation on a bank.

    1. **Dedup** — paginates through all vectors, compares embeddings pairwise,
       and deletes near-duplicates (keeping the first occurrence).
    2. **Stale archival** — if ``archive_unretrieved_after_days`` is set, deletes
       memories that have never been recalled within that window.
    3. **Entity cleanup** — if a ``graph_store`` is provided, removes entities
       that no longer link to any remaining memories.
    """
    if not hasattr(vector_store, "list_vectors"):
        logger.warning("VectorStore does not support list_vectors; skipping consolidation")
        return ConsolidationResult(duplicates_removed=0, total_scanned=0)

    duplicates_removed = 0
    stale_archived = 0
    total_scanned = 0
    seen_embeddings: list[tuple[str, list[float]]] = []
    to_delete_dedup: list[str] = []
    to_delete_stale: list[str] = []

    now = datetime.now(timezone.utc)

    offset = 0
    while True:
        batch = await vector_store.list_vectors(bank_id, offset=offset, limit=batch_size)
        if not batch:
            break

        for item in batch:
            total_scanned += 1

            # -- Dedup check --
            is_dup = False
            for _, seen_vec in seen_embeddings:
                try:
                    sim = cosine_similarity(item.vector, seen_vec)
                except ValueError:
                    continue
                if sim >= similarity_threshold:
                    to_delete_dedup.append(item.id)
                    is_dup = True
                    break

            if not is_dup:
                seen_embeddings.append((item.id, item.vector))

            # -- Stale archival check --
            if not is_dup and archive_unretrieved_after_days is not None and item.metadata:
                last_recalled = item.metadata.get("_last_recalled_at")
                created_at = item.metadata.get("_created_at")

                # Parse datetime strings
                ref_time = None
                if last_recalled:
                    ref_time = _parse_dt(last_recalled) if isinstance(last_recalled, str) else last_recalled
                elif created_at:
                    ref_time = _parse_dt(created_at) if isinstance(created_at, str) else created_at

                if ref_time and isinstance(ref_time, datetime):
                    age_days = (now - ref_time).days
                    if age_days >= archive_unretrieved_after_days:
                        to_delete_stale.append(item.id)

        offset += len(batch)
        if len(batch) < batch_size:
            break

        # Safety: prevent runaway scans
        if offset > 100000:
            logger.warning("Consolidation scan capped at 100k vectors for bank %s", bank_id)
            break

    # Delete duplicates
    if to_delete_dedup:
        for i in range(0, len(to_delete_dedup), batch_size):
            chunk = to_delete_dedup[i : i + batch_size]
            deleted = await vector_store.delete(chunk, bank_id)
            duplicates_removed += deleted

    # Delete stale memories
    if to_delete_stale:
        for i in range(0, len(to_delete_stale), batch_size):
            chunk = to_delete_stale[i : i + batch_size]
            deleted = await vector_store.delete(chunk, bank_id)
            stale_archived += deleted

    # Entity cleanup
    orphaned_removed = 0
    if graph_store and hasattr(graph_store, "remove_orphaned_entities"):
        try:
            orphaned_removed = await graph_store.remove_orphaned_entities(bank_id)
        except Exception:
            logger.warning("Entity cleanup failed for bank %s", bank_id, exc_info=True)

    return ConsolidationResult(
        duplicates_removed=duplicates_removed,
        total_scanned=total_scanned,
        stale_archived=stale_archived,
        orphaned_entities_removed=orphaned_removed,
    )


def _parse_dt(value: str) -> datetime | None:
    """Best-effort ISO datetime parse."""
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
