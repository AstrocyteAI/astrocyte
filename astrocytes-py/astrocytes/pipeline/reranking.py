"""Basic reranking — simple scoring for Phase 1.

Sync, pure computation — Rust migration candidate.
Phase 2: cross-encoder or LLM-based reranking.
"""

from __future__ import annotations

from astrocytes.pipeline.fusion import ScoredItem


def basic_rerank(items: list[ScoredItem], query: str) -> list[ScoredItem]:
    """Basic reranking using keyword overlap bonus.

    Adds a small bonus to items whose text contains query terms.
    This is a placeholder — production systems should use cross-encoders.
    """
    if not items or not query:
        return items

    query_terms = set(query.lower().split())

    reranked: list[ScoredItem] = []
    for item in items:
        item_terms = set(item.text.lower().split())
        overlap = len(query_terms & item_terms)
        bonus = overlap * 0.01  # Small bonus per matching term
        reranked.append(
            ScoredItem(
                id=item.id,
                text=item.text,
                score=item.score + bonus,
                fact_type=item.fact_type,
                metadata=item.metadata,
                tags=item.tags,
            )
        )

    reranked.sort(key=lambda x: x.score, reverse=True)
    return reranked
