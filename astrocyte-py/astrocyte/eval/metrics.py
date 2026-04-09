"""Evaluation metrics — precision, MRR, NDCG, latency percentiles.

All functions are sync, pure computation — no I/O.
"""

from __future__ import annotations

import math


def precision_at_k(relevant_ids: set[str], retrieved_ids: list[str]) -> float:
    """Fraction of retrieved items that are relevant."""
    if not retrieved_ids:
        return 0.0
    hits = sum(1 for rid in retrieved_ids if rid in relevant_ids)
    return hits / len(retrieved_ids)


def recall_hit(relevant_ids: set[str], retrieved_ids: list[str]) -> bool:
    """Whether at least one relevant item was retrieved."""
    return any(rid in relevant_ids for rid in retrieved_ids)


def reciprocal_rank(relevant_ids: set[str], retrieved_ids: list[str]) -> float:
    """1/rank of the first relevant item (0.0 if none found)."""
    for i, rid in enumerate(retrieved_ids, 1):
        if rid in relevant_ids:
            return 1.0 / i
    return 0.0


def ndcg_at_k(relevant_ids: set[str], retrieved_ids: list[str]) -> float:
    """Normalized Discounted Cumulative Gain.

    Uses binary relevance: 1 if relevant, 0 if not.
    """
    if not retrieved_ids or not relevant_ids:
        return 0.0

    # DCG
    dcg = 0.0
    for i, rid in enumerate(retrieved_ids, 1):
        rel = 1.0 if rid in relevant_ids else 0.0
        dcg += rel / math.log2(i + 1)

    # Ideal DCG (all relevant items at top)
    ideal_count = min(len(relevant_ids), len(retrieved_ids))
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_count + 1))

    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def percentile(values: list[float], p: float) -> float:
    """Compute the p-th percentile (0–100) of a list of values."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def text_overlap_score(expected_keywords: list[str], actual_text: str) -> float:
    """Score based on how many expected keywords appear in the actual text.

    Returns fraction of keywords found (0.0–1.0).
    """
    if not expected_keywords:
        return 1.0  # No expectations = pass
    actual_lower = str(actual_text).lower()
    found = sum(1 for kw in expected_keywords if str(kw).lower() in actual_lower)
    return found / len(expected_keywords)
