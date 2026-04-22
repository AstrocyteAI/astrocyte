"""Basic reranking — keyword and entity-aware scoring for Phase 1.

Sync, pure computation — Rust migration candidate.
Phase 2: cross-encoder or LLM-based reranking.
"""

from __future__ import annotations

from string import punctuation

from astrocyte.mip.schema import RerankSpec
from astrocyte.pipeline.fusion import ScoredItem

COMMON_QUESTION_WORDS: set[str] = {
    "what",
    "when",
    "where",
    "who",
    "why",
    "how",
    "which",
    "did",
    "does",
    "do",
    "is",
    "are",
    "was",
    "were",
    "has",
    "have",
    "can",
    "could",
    "would",
    "should",
    "will",
    "tell",
    "describe",
}

KEYWORD_OVERLAP_WEIGHT = 0.05
PROPER_NOUN_WEIGHT = 0.10

# Characters allowed inside proper names (apostrophes and hyphens).
# Straight apostrophe, left/right single quotation marks, and hyphen.
NAME_CONNECTOR_CHARS = ("'", "\u2018", "\u2019", "-")


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
        if not (ch.isalpha() or ch in NAME_CONNECTOR_CHARS):
            return False
    return True


def basic_rerank(
    items: list[ScoredItem],
    query: str,
    *,
    mip_rerank: RerankSpec | None = None,
) -> list[ScoredItem]:
    """Rerank items using keyword overlap and proper-noun boosting.

    Adds bonuses to items whose text contains:
    - General query terms (``keyword_weight`` per matching term)
    - Proper nouns / names from the query (``proper_noun_weight`` per match)

    Defaults come from module constants (``KEYWORD_OVERLAP_WEIGHT`` /
    ``PROPER_NOUN_WEIGHT``); a MIP ``RerankSpec`` from the active routing
    decision can override either weight on a per-call basis without mutating
    module state. ``None`` fields fall through to the default.

    This is a heuristic — production systems should use cross-encoders.
    """
    if not items or not query:
        return items

    keyword_weight = (
        mip_rerank.keyword_weight
        if mip_rerank is not None and mip_rerank.keyword_weight is not None
        else KEYWORD_OVERLAP_WEIGHT
    )
    proper_noun_weight = (
        mip_rerank.proper_noun_weight
        if mip_rerank is not None and mip_rerank.proper_noun_weight is not None
        else PROPER_NOUN_WEIGHT
    )

    # Tokenize query once; filter common question words for overlap scoring
    query_terms = {t for t in _tokenize_terms(query) if t not in COMMON_QUESTION_WORDS}

    # Detect proper nouns from all words.
    # Matches: Title Case ("Alice"), ALL CAPS ("USA"), and lowercase names that
    # appear as query terms but aren't common words (caught by _is_name_token).
    proper_nouns: set[str] = set()
    for w in query.split():
        cleaned = w.strip(punctuation)
        if not cleaned or cleaned.lower() in COMMON_QUESTION_WORDS:
            continue
        is_proper = (
            cleaned.istitle()  # "Alice"
            or (cleaned.isupper() and len(cleaned) >= 2)  # "USA", "AI"
        )
        if is_proper and _is_name_token(cleaned):
            proper_nouns.add(cleaned.lower())

    # Pre-compute tokenized terms for all items to avoid repeated work.
    item_terms_by_item = [(item, set(_tokenize_terms(item.text))) for item in items]

    return sorted(
        (
            ScoredItem(
                id=item.id,
                text=item.text,
                score=item.score
                + len(query_terms & item_terms) * keyword_weight
                + len(proper_nouns & item_terms) * proper_noun_weight,
                fact_type=item.fact_type,
                metadata=item.metadata,
                tags=item.tags,
            )
            for item, item_terms in item_terms_by_item
        ),
        key=lambda x: x.score,
        reverse=True,
    )
