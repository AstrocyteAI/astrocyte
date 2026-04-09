"""Basic reranking — keyword and entity-aware scoring for Phase 1.

Sync, pure computation — Rust migration candidate.
Phase 2: cross-encoder or LLM-based reranking.
"""

from __future__ import annotations

from string import punctuation

from astrocyte.pipeline.fusion import ScoredItem


def _tokenize_terms(text: str) -> list[str]:
    """Tokenize text consistently for keyword and item matching.

    - Split on whitespace
    - Strip leading/trailing punctuation
    - Lowercase
    - Drop empty tokens
    """
    return [t for t in (w.strip(punctuation).lower() for w in text.split()) if t]


def _is_name_token(token: str) -> bool:
    """Check if a token looks like a proper name, allowing apostrophes and hyphens.

    Accepts: "Alice", "O'Brien", "Mary-Ann", "Jean-Paul"
    Rejects: "--", "'hello", "123", ""
    """
    if not token:
        return False
    # Must start and end with a letter
    if not token[0].isalpha() or not token[-1].isalpha():
        return False
    # Interior characters must be letters, apostrophes, or hyphens
    for ch in token:
        if not (ch.isalpha() or ch in ("'", "'", "-")):
            return False
    return True


def basic_rerank(items: list[ScoredItem], query: str) -> list[ScoredItem]:
    """Rerank items using keyword overlap and proper-noun boosting.

    Adds bonuses to items whose text contains:
    - General query terms (0.05 per matching term)
    - Proper nouns / names from the query (0.10 per matching proper noun)

    This is a heuristic — production systems should use cross-encoders.
    """
    if not items or not query:
        return items

    query_terms = set(_tokenize_terms(query))
    # Proper nouns: capitalized words that aren't sentence starters (rough heuristic).
    # Strip edge punctuation so "John," and "(Alice)." are still detected.
    query_words = query.split()
    proper_nouns: set[str] = set()
    for w in query_words[1:]:  # skip first word (always capitalized)
        cleaned = w.strip(punctuation)
        if cleaned and cleaned.istitle() and _is_name_token(cleaned):
            proper_nouns.add(cleaned.lower())
    # Also include first word if it looks like a name (not a common question word)
    if query_words:
        first = query_words[0].strip(punctuation)
        if first and first.istitle() and _is_name_token(first) and first.lower() not in {
            "what", "when", "where", "who", "why", "how", "which",
            "did", "does", "do", "is", "are", "was", "were", "has", "have",
            "can", "could", "would", "should", "will", "tell", "describe",
        }:
            proper_nouns.add(first.lower())

    reranked: list[ScoredItem] = []
    for item in items:
        item_terms = set(_tokenize_terms(item.text))
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
