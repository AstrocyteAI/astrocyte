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
QUERY_INTERACTION_WEIGHT = 0.35
QUERY_PHRASE_WEIGHT = 0.08
QUERY_NAME_MATCH_WEIGHT = 0.25
QUERY_NAME_MISS_PENALTY = 0.20
COMPILED_LAYER_WEIGHT = 0.30
OBSERVATION_LAYER_WEIGHT = 0.20
# Observation proof-count boost: each additional confirming memory adds this
# to the score, capped at OBSERVATION_PROOF_CAP × weight.  A 5-evidence
# observation gets a +0.10 bonus over a single-evidence raw memory.
OBSERVATION_PROOF_WEIGHT = 0.025
OBSERVATION_PROOF_CAP = 4  # clamp at 4 additional proofs

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


def _content_terms(text: str) -> set[str]:
    return {t for t in _tokenize_terms(text) if t not in COMMON_QUESTION_WORDS and len(t) > 2}


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
                + len(proper_nouns & item_terms) * proper_noun_weight
                + _observation_proof_boost(item),
                fact_type=item.fact_type,
                metadata=item.metadata,
                tags=item.tags,
                memory_layer=item.memory_layer,
                retained_at=item.retained_at,
            )
            for item, item_terms in item_terms_by_item
        ),
        key=lambda x: x.score,
        reverse=True,
    )


def cross_encoder_like_rerank(
    items: list[ScoredItem],
    query: str,
) -> list[ScoredItem]:
    """Final precision rerank using query-item interaction features.

    This is a deterministic local stand-in for a cross-encoder: it scores each
    query/memory pair jointly, then applies entity/person and memory-layer
    signals. It is intentionally cheap enough to run before ``reflect()``
    synthesis, where precision matters more than broad candidate coverage.
    """
    if not items or not query:
        return items

    query_terms = _content_terms(query)
    query_names = _proper_names(query)
    query_bigrams = _bigrams(_tokenize_terms(query))

    scored: list[ScoredItem] = []
    for item in items:
        item_terms = _content_terms(item.text)
        item_names = _proper_names(item.text)
        overlap = len(query_terms & item_terms) / max(len(query_terms), 1)
        phrase_hits = len(query_bigrams & _bigrams(_tokenize_terms(item.text)))

        score = item.score
        score += overlap * QUERY_INTERACTION_WEIGHT
        score += min(phrase_hits, 3) * QUERY_PHRASE_WEIGHT
        score += _layer_boost(item)

        if query_names:
            if query_names & item_names:
                score += QUERY_NAME_MATCH_WEIGHT
            elif item_names:
                score -= QUERY_NAME_MISS_PENALTY

        scored.append(
            ScoredItem(
                id=item.id,
                text=item.text,
                score=max(score, 0.0),
                fact_type=item.fact_type,
                metadata=item.metadata,
                tags=item.tags,
                memory_layer=item.memory_layer,
                retained_at=item.retained_at,
            )
        )

    return sorted(scored, key=lambda x: x.score, reverse=True)


def _proper_names(text: str) -> set[str]:
    names: set[str] = set()
    for word in text.split():
        cleaned = word.strip(punctuation)
        if cleaned and cleaned.istitle() and _is_name_token(cleaned):
            names.add(cleaned.lower())
    return names


def _bigrams(tokens: list[str]) -> set[tuple[str, str]]:
    return set(zip(tokens, tokens[1:], strict=False))


def _layer_boost(item: ScoredItem) -> float:
    if item.fact_type == "wiki" or item.memory_layer == "compiled":
        return COMPILED_LAYER_WEIGHT
    if item.fact_type == "observation" or item.memory_layer == "observation":
        return OBSERVATION_LAYER_WEIGHT + _observation_proof_boost(item)
    return 0.0


def _observation_proof_boost(item: ScoredItem) -> float:
    """Additive boost for observation items proportional to their proof count.

    A single-evidence observation (``_obs_proof_count=1``) gets +0.0.
    Each additional corroborating memory adds ``OBSERVATION_PROOF_WEIGHT``,
    capped at ``OBSERVATION_PROOF_CAP`` extra proofs (+0.10 total).
    Raw memories (no ``_obs_proof_count``) are unaffected.
    """
    if item.fact_type != "observation" or not item.metadata:
        return 0.0
    proof = item.metadata.get("_obs_proof_count", 1)
    try:
        extra = max(0, int(proof) - 1)
    except (TypeError, ValueError):
        return 0.0
    return min(extra, OBSERVATION_PROOF_CAP) * OBSERVATION_PROOF_WEIGHT
