"""Query intent classification — lightweight heuristic for biasing retrieval.

Inspired by EdgeQuake's intent-based mode selection (see
``docs/_design/platform-positioning.md`` §EdgeQuake). Given a natural-
language query, classify it into one of five intents so the retrieval
layer can bias RRF weights, pick specific strategies, or adjust the
temporal half-life.

Pure, sync, zero LLM. Regex-driven so it's fast (sub-millisecond per
query) and deterministic. When the classifier is uncertain, it returns
:class:`QueryIntent.UNKNOWN` — callers must fall back to the default
multi-strategy blend rather than making a judgment call from guessed
signal.

References:

- EdgeQuake: ``edgequake-query/src/keywords/intent.rs`` — regex-based
  intent → query-mode mapping (Factual / Relational / Comparative /
  Procedural / Exploratory).
- Hindsight: forced retrieval hierarchy in reflect — prioritizes
  consolidated observations over raw facts. Our intent is a recall-side
  analogue, biasing which *retrieval strategy* gets weight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class QueryIntent(str, Enum):
    """Coarse classification of query purpose.

    Values map to retrieval strategy biases:

    - ``FACTUAL``: "what / who / when / where / how many" — single fact
      lookup. Semantic + keyword both contribute; graph rarely helps;
      temporal matters only if the query cites a time.
    - ``RELATIONAL``: "how does X relate to Y", "connection between" —
      graph traversal is the primary signal; semantic as fallback.
    - ``COMPARATIVE``: "X vs Y", "difference between", "better than" —
      benefits from broader keyword recall to surface both sides.
    - ``PROCEDURAL``: "how to / steps / procedure" — semantic does well;
      keyword secondary for specific tool names.
    - ``TEMPORAL``: "when / recently / last week / before / after" — the
      temporal strategy gets a weight boost.
    - ``EXPLORATORY``: "tell me about / what about / summary of" — all
      strategies contribute; blend favors diversity over precision.
    - ``UNKNOWN``: no confident signal. Callers should NOT bias weights
      on UNKNOWN — silently fall back to the default multi-strategy
      blend.
    """

    FACTUAL = "factual"
    RELATIONAL = "relational"
    COMPARATIVE = "comparative"
    PROCEDURAL = "procedural"
    TEMPORAL = "temporal"
    EXPLORATORY = "exploratory"
    UNKNOWN = "unknown"


@dataclass
class QueryIntentResult:
    """Classification result with a confidence signal."""

    intent: QueryIntent
    confidence: float  # 0.0 – 1.0
    matched_signals: list[str]  # Which patterns triggered — useful for debug


# ---------------------------------------------------------------------------
# Pattern vocabulary
# ---------------------------------------------------------------------------
# Each pattern is a (regex, weight) pair. Weight accumulates into the
# intent's score; regexes should be cheap (no backtracking) and specific
# enough to avoid cross-intent overlap.

_TEMPORAL_PATTERNS: tuple[tuple[str, float], ...] = (
    (r"\b(when|recently|lately|yesterday|today|last\s+(week|month|year|night|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b", 0.7),
    (r"\b(previous|last)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", 0.6),
    (r"\b(two|three|four|\d+)\s+weekends?\s+(before|ago|earlier)\b", 0.6),
    (r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(days?|weeks?|months?|years?)\s+(before|ago|earlier)\b", 0.5),
    (r"\bthe\s+week\s+before\b", 0.5),
    (r"\b(before|after|since|until|during)\b", 0.4),
    (r"\b\d{4}\b", 0.2),  # bare year like "2023"
    (r"\b(earlier|later|latest|oldest|newest|first|last)\b", 0.3),
)

_RELATIONAL_PATTERNS: tuple[tuple[str, float], ...] = (
    (r"\b(relate|relat(ed|ion|ionship)|connect(ed|ion)?|linked?\s+to)\b", 0.8),
    (r"\bbetween\s+\w+\s+and\s+\w+", 0.5),
    (r"\b(depend\w*|influence|cause|effect|impact)\b", 0.3),
)

_COMPARATIVE_PATTERNS: tuple[tuple[str, float], ...] = (
    (r"\b(vs|versus)\b", 0.9),
    (r"\bcompare(d)?\b|\bcomparison\b", 0.8),
    (r"\bdifference\s+(between|from)\b|\bdiffer\w*\b", 0.7),
    (r"\b(better|worse|more|less|greater|smaller)\s+than\b", 0.6),
    (r"\bsimilar\s+to\b", 0.4),
)

_PROCEDURAL_PATTERNS: tuple[tuple[str, float], ...] = (
    (r"\bhow\s+(to|do\s+i|can\s+i|should\s+i)\b", 0.8),
    (r"\b(steps?|procedure|process|workflow|tutorial|guide)\b", 0.5),
    (r"\b(configure|install|setup|set\s+up|enable|disable)\b", 0.4),
)

_FACTUAL_PATTERNS: tuple[tuple[str, float], ...] = (
    (r"^\s*(what|who|where|which|how\s+many|how\s+much)\b", 0.6),
    (r"\bis\s+the\b|\bare\s+the\b", 0.2),
)

_EXPLORATORY_PATTERNS: tuple[tuple[str, float], ...] = (
    (r"\b(tell\s+me\s+about|what\s+about|anything\s+about)\b", 0.8),
    (r"\b(summary|overview|describe|explain)\b", 0.5),
)


# Compile once at import. Patterns are case-insensitive.
_COMPILED: dict[QueryIntent, list[tuple[re.Pattern[str], float]]] = {
    QueryIntent.TEMPORAL: [(re.compile(p, re.IGNORECASE), w) for p, w in _TEMPORAL_PATTERNS],
    QueryIntent.RELATIONAL: [(re.compile(p, re.IGNORECASE), w) for p, w in _RELATIONAL_PATTERNS],
    QueryIntent.COMPARATIVE: [(re.compile(p, re.IGNORECASE), w) for p, w in _COMPARATIVE_PATTERNS],
    QueryIntent.PROCEDURAL: [(re.compile(p, re.IGNORECASE), w) for p, w in _PROCEDURAL_PATTERNS],
    QueryIntent.FACTUAL: [(re.compile(p, re.IGNORECASE), w) for p, w in _FACTUAL_PATTERNS],
    QueryIntent.EXPLORATORY: [(re.compile(p, re.IGNORECASE), w) for p, w in _EXPLORATORY_PATTERNS],
}


#: Confidence threshold below which the classifier returns UNKNOWN.
#: Tuned so that single weak signals (e.g. only a bare year) don't
#: categorize a query. Callers rely on UNKNOWN → default blend, so this
#: threshold directly shapes when intent-aware biasing engages.
CONFIDENCE_THRESHOLD = 0.4


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_query_intent(query: str) -> QueryIntentResult:
    """Classify a query into a :class:`QueryIntent`.

    Scans the query against each intent's regex bank, accumulates weights,
    and returns the highest-scoring intent — provided its score crosses
    :data:`CONFIDENCE_THRESHOLD`. Ties break toward the intent with more
    matched signals (more diverse evidence).

    A query can legitimately combine intents (e.g. "when did Alice and
    Bob start working together" is both TEMPORAL and RELATIONAL). This
    classifier picks the dominant one; downstream callers that want
    richer signals can use :func:`classify_all_intents` below.
    """
    query_clean = (query or "").strip()
    if not query_clean:
        return QueryIntentResult(QueryIntent.UNKNOWN, 0.0, [])

    scores: dict[QueryIntent, float] = {}
    signals: dict[QueryIntent, list[str]] = {}

    for intent, patterns in _COMPILED.items():
        score = 0.0
        hits: list[str] = []
        for pattern, weight in patterns:
            if pattern.search(query_clean):
                score += weight
                hits.append(pattern.pattern)
        if score > 0:
            scores[intent] = score
            signals[intent] = hits

    if not scores:
        return QueryIntentResult(QueryIntent.UNKNOWN, 0.0, [])

    # Pick the intent with the highest score. Ties broken by number of
    # matched signals, then alphabetically by intent name for stability.
    def _key(intent: QueryIntent) -> tuple[float, int, str]:
        return (scores[intent], len(signals[intent]), intent.value)

    best = max(scores.keys(), key=_key)
    confidence = min(scores[best], 1.0)  # Cap at 1.0 for downstream reasoning.

    if confidence < CONFIDENCE_THRESHOLD:
        return QueryIntentResult(QueryIntent.UNKNOWN, confidence, signals[best])

    return QueryIntentResult(best, confidence, signals[best])


def classify_all_intents(query: str) -> dict[QueryIntent, float]:
    """Return full score map — useful when a caller wants to blend
    strategy weights by multi-intent evidence rather than pick a single
    dominant intent.

    Scores are not normalized — raw pattern-weight sums, capped at 1.0
    per intent. Empty dict means no signal.
    """
    query_clean = (query or "").strip()
    if not query_clean:
        return {}

    scores: dict[QueryIntent, float] = {}
    for intent, patterns in _COMPILED.items():
        score = 0.0
        for pattern, weight in patterns:
            if pattern.search(query_clean):
                score += weight
        if score > 0:
            scores[intent] = min(score, 1.0)
    return scores


# ---------------------------------------------------------------------------
# Strategy weighting — how classifier output biases retrieval
# ---------------------------------------------------------------------------


@dataclass
class StrategyWeights:
    """RRF input weights keyed by retrieval strategy name.

    After RRF fusion produces ranked items, each strategy's contribution
    is multiplied by its weight before final sort. A weight of 1.0 is
    neutral; > 1.0 amplifies that strategy; 0.0 mutes it.
    """

    semantic: float = 1.0
    keyword: float = 1.0
    graph: float = 1.0
    temporal: float = 1.0


#: Default strategy weights per intent. Conservative biases — no strategy
#: is fully muted (everything fuses; biases shift the balance). Tuned by
#: the qualitative mapping in :class:`QueryIntent` docstrings.
INTENT_STRATEGY_WEIGHTS: dict[QueryIntent, StrategyWeights] = {
    QueryIntent.FACTUAL: StrategyWeights(semantic=1.2, keyword=1.2, graph=0.7, temporal=0.8),
    QueryIntent.RELATIONAL: StrategyWeights(semantic=0.9, keyword=0.8, graph=1.5, temporal=0.8),
    QueryIntent.COMPARATIVE: StrategyWeights(semantic=1.0, keyword=1.3, graph=1.0, temporal=0.8),
    QueryIntent.PROCEDURAL: StrategyWeights(semantic=1.2, keyword=1.0, graph=0.8, temporal=0.8),
    QueryIntent.TEMPORAL: StrategyWeights(semantic=0.9, keyword=0.9, graph=0.8, temporal=1.5),
    QueryIntent.EXPLORATORY: StrategyWeights(semantic=1.0, keyword=1.0, graph=1.0, temporal=1.0),
    QueryIntent.UNKNOWN: StrategyWeights(),  # Neutral — no bias on guess.
}


def weights_for_intent(intent: QueryIntent) -> StrategyWeights:
    """Look up canonical :class:`StrategyWeights` for an intent."""
    return INTENT_STRATEGY_WEIGHTS.get(intent, StrategyWeights())
