"""Basic reranking — keyword and entity-aware scoring for Phase 1.

Sync, pure computation — Rust migration candidate.
Phase 2: cross-encoder or LLM-based reranking.
"""

from __future__ import annotations

import json
from string import punctuation
from typing import TYPE_CHECKING

from astrocyte.mip.schema import RerankSpec
from astrocyte.pipeline.fusion import ScoredItem
from astrocyte.types import Message

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider

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
SESSION_DIVERSITY_PENALTY = 0.08
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
                chunk_id=getattr(item, "chunk_id", None),
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
        item_names = _candidate_names(item)
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
                chunk_id=getattr(item, "chunk_id", None),
            )
        )

    return sorted(scored, key=lambda x: x.score, reverse=True)


async def llm_pairwise_rerank(
    items: list[ScoredItem],
    query: str,
    llm_provider: LLMProvider,
    *,
    top_n: int = 30,
    keep_n: int | None = None,
) -> list[ScoredItem]:
    """Use the configured LLM as a lightweight listwise reranker.

    The prompt asks for candidate IDs in descending relevance order. If the LLM
    response is missing or malformed, the deterministic local reranker is used.
    """
    if not items or not query:
        return items
    candidates = cross_encoder_like_rerank(items[:top_n], query)
    remainder = items[top_n:]
    keep = keep_n or len(candidates)

    prompt_lines = [
        "Rank the memory candidates by how directly they answer the query.",
        "Penalize wrong-person or wrong-premise candidates.",
        "Return JSON only: {\"ranked_ids\": [\"id1\", \"id2\"]}.",
        "",
        f"Query: {query}",
        "",
        "Candidates:",
    ]
    for item in candidates:
        prompt_lines.append(f"- id={item.id} score={item.score:.4f}: {item.text[:500]}")

    try:
        completion = await llm_provider.complete(
            [
                Message(role="system", content="You are a strict memory reranker."),
                Message(role="user", content="\n".join(prompt_lines)),
            ],
            max_tokens=512,
            temperature=0.0,
        )
        ranked_ids = _parse_ranked_ids(completion.text)
    except Exception:
        ranked_ids = []

    if not ranked_ids:
        return apply_context_diversity(candidates, query)[:keep] + remainder

    by_id = {item.id: item for item in candidates}
    ordered: list[ScoredItem] = []
    seen: set[str] = set()
    for item_id in ranked_ids:
        item = by_id.get(item_id)
        if item is not None and item_id not in seen:
            ordered.append(item)
            seen.add(item_id)
    ordered.extend(item for item in candidates if item.id not in seen)
    return apply_context_diversity(ordered, query)[:keep] + remainder


def apply_context_diversity(items: list[ScoredItem], query: str) -> list[ScoredItem]:
    """Softly penalize repeated sessions unless the query asks for aggregation."""
    if not items:
        return items
    if _is_aggregate_query(query):
        return items

    session_counts: dict[str, int] = {}
    diversified: list[ScoredItem] = []
    for item in items:
        session = str((item.metadata or {}).get("session_id") or "")
        penalty = 0.0
        if session:
            seen = session_counts.get(session, 0)
            penalty = seen * SESSION_DIVERSITY_PENALTY
            session_counts[session] = seen + 1
        diversified.append(
            ScoredItem(
                id=item.id,
                text=item.text,
                score=max(item.score - penalty, 0.0),
                fact_type=item.fact_type,
                metadata=item.metadata,
                tags=item.tags,
                memory_layer=item.memory_layer,
                occurred_at=item.occurred_at,
                retained_at=item.retained_at,
                chunk_id=getattr(item, "chunk_id", None),
            )
        )
    return sorted(diversified, key=lambda item: item.score, reverse=True)


def _proper_names(text: str) -> set[str]:
    names: set[str] = set()
    for word in text.split():
        cleaned = word.strip(punctuation)
        if cleaned and cleaned.istitle() and _is_name_token(cleaned):
            names.add(cleaned.lower())
    return names


def _candidate_names(item: ScoredItem) -> set[str]:
    names = _proper_names(item.text)
    metadata = item.metadata or {}
    for key in ("locomo_speakers", "locomo_persons", "speakers", "person"):
        raw = metadata.get(key)
        if not raw:
            continue
        for part in str(raw).replace("|", ",").split(","):
            cleaned = part.strip().lower()
            if cleaned:
                names.add(cleaned)
    return names


def _parse_ranked_ids(text: str) -> list[str]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("```"))
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    ids = parsed.get("ranked_ids") if isinstance(parsed, dict) else None
    if not isinstance(ids, list):
        return []
    return [str(item) for item in ids if item]


def _is_aggregate_query(query: str) -> bool:
    query_l = (query or "").lower()
    return any(term in query_l for term in ("how many", "what are", "list", "all ", "which "))


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
