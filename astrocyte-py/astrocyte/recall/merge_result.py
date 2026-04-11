"""Merge federated / external MemoryHit rows into a RecallResult (engine & tiered paths)."""

from __future__ import annotations

from astrocyte.types import MemoryHit, RecallResult


def merge_external_into_recall_result(
    result: RecallResult,
    external: list[MemoryHit],
    max_results: int,
) -> RecallResult:
    """Combine external hits with ``result.hits`` by score, dedupe by text, trim to ``max_results``."""
    combined = list(external) + list(result.hits)
    combined.sort(key=lambda h: h.score, reverse=True)
    seen: set[str] = set()
    deduped: list[MemoryHit] = []
    for h in combined:
        if h.text in seen:
            continue
        seen.add(h.text)
        deduped.append(h)
        if len(deduped) >= max_results:
            break
    return RecallResult(
        hits=deduped,
        total_available=result.total_available + len(external),
        truncated=result.truncated,
        trace=result.trace,
    )
