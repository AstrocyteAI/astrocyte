"""Basic reranking — keyword and entity-aware scoring for Phase 1.

Sync, pure computation — Rust migration candidate.
Phase 2: cross-encoder or LLM-based reranking.
"""

from __future__ import annotations

from string import punctuation

from astrocyte.pipeline.fusion import ScoredItem

COMMON_QUESTION_WORDS: set[str] = {
    "what", "when", "where", "who", "why", "how", "which",
    "did", "does", "do", "is", "are", "was", "were", "has", "have",
    "can", "could", "would", "should", "will", "tell", "describe",
}

KEYWORD_OVERLAP_WEIGHT = 0.05
PROPER_NOUN_WEIGHT = 0.10


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
        if not (ch.isalpha() or ch in ("'", "\u2018", "\u2019", "-")):
            return False
    return True


def basic_rerank(items: list[ScoredItem], query: str) -> list[ScoredItem]:
    """Rerank items using keyword overlap and proper-noun boosting.

    Adds bonuses to items whose text contains:
    - General query terms (KEYWORD_OVERLAP_WEIGHT per matching term)
    - Proper nouns / names from the query (PROPER_NOUN_WEIGHT per match)

    This is a heuristic — production systems should use cross-encoders.
    """
    if not items or not query:
        return items

    # Tokenize query once; filter common question words for overlap scoring
    query_terms = {
        t for t in _tokenize_terms(query) if t not in COMMON_QUESTION_WORDS
    }

    # Detect proper nouns from all words (check capitalization + name structure).
    # Also exclude common question words so "Who" isn't treated as a name.
    proper_nouns: set[str] = set()
    for w in query.split():
        cleaned = w.strip(punctuation)
        if (
            cleaned
            and cleaned.istitle()
            and _is_name_token(cleaned)
            and cleaned.lower() not in COMMON_QUESTION_WORDS
        ):
            proper_nouns.add(cleaned.lower())

    reranked: list[ScoredItem] = []
    for item in items:
        item_terms = set(_tokenize_terms(item.text))
        # General keyword overlap
        overlap = len(query_terms & item_terms)
        bonus = overlap * KEYWORD_OVERLAP_WEIGHT
        # Proper noun / name boost
        name_matches = len(proper_nouns & item_terms)
        bonus += name_matches * PROPER_NOUN_WEIGHT
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
