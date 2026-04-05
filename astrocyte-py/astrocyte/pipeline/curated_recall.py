"""Curated recall — post-retrieval re-scoring by freshness, reliability, and salience.

Applied after retrieval and fusion, before returning to the caller.
Provider-agnostic — works with any Tier 1 or Tier 2 backend.

Sync, pure computation — Rust migration candidate.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from astrocyte.types import MemoryHit


def curate_recall_hits(
    hits: list[MemoryHit],
    *,
    freshness_weight: float = 0.3,
    reliability_weight: float = 0.2,
    salience_weight: float = 0.2,
    original_score_weight: float = 0.3,
    freshness_half_life_days: float = 30.0,
    min_score: float | None = None,
) -> list[MemoryHit]:
    """Re-score recall hits by freshness, reliability, and salience.

    Combines original retrieval score with:
    - Freshness: exponential decay based on occurred_at
    - Reliability: metadata-based scoring (source trust, fact_type)
    - Salience: memory_layer boosting (models > observations > facts)

    Returns re-ranked hits. Optionally filters below min_score.
    Sync, pure computation — Rust migration candidate.
    """
    if not hits:
        return []

    now = datetime.now(timezone.utc)
    half_life_seconds = freshness_half_life_days * 86400.0

    scored: list[tuple[float, MemoryHit]] = []

    for hit in hits:
        # Freshness: decay from occurred_at (or assume recent if missing)
        if hit.occurred_at:
            age_seconds = max(0.0, (now - hit.occurred_at).total_seconds())
            freshness = math.exp(-0.693 * age_seconds / max(half_life_seconds, 1.0))
        else:
            freshness = 0.5  # Unknown age → neutral

        # Reliability: based on fact_type and source
        reliability = _reliability_score(hit)

        # Salience: based on memory_layer
        salience = _salience_score(hit)

        # Composite score
        composite = (
            original_score_weight * hit.score
            + freshness_weight * freshness
            + reliability_weight * reliability
            + salience_weight * salience
        )

        scored.append((composite, hit))

    # Sort by composite score descending
    scored.sort(key=lambda x: x[0], reverse=True)

    # Update scores on hits
    result: list[MemoryHit] = []
    for composite, hit in scored:
        if min_score is not None and composite < min_score:
            continue
        # Create new MemoryHit with updated score (preserve all other fields)
        from dataclasses import replace

        result.append(replace(hit, score=composite))

    return result


def _reliability_score(hit: MemoryHit) -> float:
    """Score reliability based on fact_type and metadata.

    Higher for experience (first-hand) > world (general) > observation (derived).
    """
    type_scores = {
        "experience": 0.9,
        "world": 0.7,
        "observation": 0.6,
        "model": 0.5,
    }
    base = type_scores.get(hit.fact_type or "", 0.5)

    # Boost if source is specified (provenance exists)
    if hit.source:
        base = min(1.0, base + 0.1)

    return base


def _salience_score(hit: MemoryHit) -> float:
    """Score salience based on memory_layer.

    Models > observations > facts (higher layers = more curated knowledge).
    """
    layer_scores = {
        "model": 1.0,
        "observation": 0.75,
        "fact": 0.5,
    }
    return layer_scores.get(hit.memory_layer or "", 0.5)
