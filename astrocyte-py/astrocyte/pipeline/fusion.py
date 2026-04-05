"""Reciprocal Rank Fusion (RRF) — merge results from multiple retrieval strategies.

Sync, pure computation — Rust migration candidate.
See docs/_design/built-in-pipeline.md section 3.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScoredItem:
    """A scored item from any retrieval strategy."""

    id: str
    text: str
    score: float
    fact_type: str | None = None
    metadata: dict[str, str | int | float | bool | None] | None = None
    tags: list[str] | None = None
    memory_layer: str | None = None  # "fact", "observation", "model"


def rrf_fusion(
    ranked_lists: list[list[ScoredItem]],
    k: int = 60,
) -> list[ScoredItem]:
    """Reciprocal Rank Fusion across multiple ranked result lists.

    RRF score = Σ(1 / (k + rank)) for each list where item appears.
    Items are deduplicated by id.

    Sync, pure computation — Rust migration candidate.
    """
    if not ranked_lists:
        return []

    # Accumulate RRF scores by item id
    scores: dict[str, float] = {}
    items: dict[str, ScoredItem] = {}

    for ranked_list in ranked_lists:
        for rank, item in enumerate(ranked_list):
            rrf_score = 1.0 / (k + rank + 1)  # rank is 0-indexed, add 1
            scores[item.id] = scores.get(item.id, 0.0) + rrf_score
            # Keep the item with the highest original score
            if item.id not in items or item.score > items[item.id].score:
                items[item.id] = item

    # Sort by RRF score descending
    sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)

    # Build result with RRF score replacing original score
    result: list[ScoredItem] = []
    for item_id in sorted_ids:
        item = items[item_id]
        result.append(
            ScoredItem(
                id=item.id,
                text=item.text,
                score=scores[item_id],
                fact_type=item.fact_type,
                metadata=item.metadata,
                tags=item.tags,
                memory_layer=item.memory_layer,
            )
        )

    return result


def layer_weighted_rrf_fusion(
    ranked_lists: list[list[ScoredItem]],
    k: int = 60,
    layer_weights: dict[str, float] | None = None,
) -> list[ScoredItem]:
    """RRF fusion with optional layer-based score boosting.

    After standard RRF, multiplies each item's score by the weight
    for its memory_layer. Items with no layer get weight 1.0.

    layer_weights example: {"fact": 1.0, "observation": 1.5, "model": 2.0}
    Higher layers (models) are boosted above raw facts.

    Sync, pure computation — Rust migration candidate.
    """
    fused = rrf_fusion(ranked_lists, k=k)

    if not layer_weights:
        return fused

    # Apply layer weights
    for item in fused:
        weight = layer_weights.get(item.memory_layer or "", 1.0)
        item.score *= weight

    # Re-sort by weighted score
    fused.sort(key=lambda x: x.score, reverse=True)
    return fused
