"""Basic reranking — keyword and entity-aware scoring for Phase 1.

Sync, pure computation — Rust migration candidate.
Phase 2: cross-encoder or LLM-based reranking.
"""

from __future__ import annotations

from astrocyte.pipeline.fusion import ScoredItem


def basic_rerank(items: list[ScoredItem], query: str) -> list[ScoredItem]:
    """Rerank items using keyword overlap and proper-noun boosting.

    Adds bonuses to items whose text contains:
    - General query terms (0.05 per matching term)
    - Proper nouns / names from the query (0.10 per matching proper noun)

    This is a heuristic — production systems should use cross-encoders.
    """
    if not items or not query:
        return items

    query_terms = set(query.lower().split())
    # Proper nouns: capitalized words that aren't sentence starters (rough heuristic)
    query_words = query.split()
    proper_nouns = {
        w.lower()
        for w in query_words[1:]  # skip first word (always capitalized)
        if w and w[0].isupper() and w.isalpha()
    }
    # Also include first word if it looks like a name (not a common question word)
    if query_words:
        first = query_words[0]
        if first[0].isupper() and first.lower() not in {
            "what", "when", "where", "who", "why", "how", "which",
            "did", "does", "do", "is", "are", "was", "were", "has", "have",
            "can", "could", "would", "should", "will", "tell", "describe",
        }:
            proper_nouns.add(first.lower())

    reranked: list[ScoredItem] = []
    for item in items:
        item_terms = set(item.text.lower().split())
        # General keyword overlap
        overlap = len(query_terms & item_terms)
        bonus = overlap * 0.05
        # Proper noun / name boost
        name_matches = len(proper_nouns & item_terms)
        bonus += name_matches * 0.10
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
