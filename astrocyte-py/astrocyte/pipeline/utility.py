"""Utility scoring — per-memory usage tracking and composite scoring.

Combines recency, frequency, relevance, and freshness into a 0-1 score
that drives TTL decisions, recall ranking boosts, and bank health metrics.

Sync, self-contained — Rust migration candidate.
Inspired by ByteRover's lifecycle metadata and Hindsight's consolidation quality.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass
class UtilityScore:
    """Composite utility score with individual components."""

    recency: float  # 0-1: how recently was this memory recalled
    frequency: float  # 0-1: how often is it recalled
    relevance: float  # 0-1: average relevance score when recalled
    freshness: float  # 0-1: how new is the memory
    composite: float  # 0-1: weighted combination


@dataclass
class _MemoryStats:
    recall_count: int = 0
    last_recalled_at: float = 0.0  # monotonic time
    total_relevance: float = 0.0
    created_at: float = 0.0  # monotonic time


def compute_utility(
    recall_count: int,
    last_recalled_seconds_ago: float,
    avg_relevance: float,
    created_seconds_ago: float,
    *,
    recency_half_life_days: float = 7.0,
    max_frequency: int = 100,
    weight_recency: float = 0.3,
    weight_frequency: float = 0.2,
    weight_relevance: float = 0.3,
    weight_freshness: float = 0.2,
) -> UtilityScore:
    """Compute composite utility score for a memory.

    All inputs are non-negative. Returns UtilityScore with components in [0, 1].
    Sync, pure computation — Rust migration candidate.
    """
    half_life_seconds = recency_half_life_days * 86400.0

    # Recency: exponential decay from last recall (1.0 = just recalled, decays to 0)
    if last_recalled_seconds_ago <= 0:
        recency = 1.0
    else:
        recency = math.exp(-0.693 * last_recalled_seconds_ago / max(half_life_seconds, 1.0))

    # Frequency: normalized recall count (capped at max_frequency)
    frequency = min(recall_count / max(max_frequency, 1), 1.0)

    # Relevance: average score when recalled (already 0-1)
    relevance = max(0.0, min(1.0, avg_relevance))

    # Freshness: how new the memory is (exponential decay from creation)
    if created_seconds_ago <= 0:
        freshness = 1.0
    else:
        freshness = math.exp(-0.693 * created_seconds_ago / max(half_life_seconds * 4, 1.0))

    # Composite: weighted sum
    composite = (
        weight_recency * recency
        + weight_frequency * frequency
        + weight_relevance * relevance
        + weight_freshness * freshness
    )
    # Normalize to [0, 1]
    total_weight = weight_recency + weight_frequency + weight_relevance + weight_freshness
    if total_weight > 0:
        composite /= total_weight

    return UtilityScore(
        recency=recency,
        frequency=frequency,
        relevance=relevance,
        freshness=freshness,
        composite=composite,
    )


class UtilityTracker:
    """Per-memory usage tracking for utility scoring.

    Maintains recall counts, timestamps, and relevance scores in memory.
    LRU eviction when over capacity.

    Sync, self-contained — Rust migration candidate.
    """

    def __init__(
        self,
        max_entries: int = 10000,
        recency_half_life_days: float = 7.0,
    ) -> None:
        self.max_entries = max_entries
        self.recency_half_life_days = recency_half_life_days
        self._stats: OrderedDict[str, _MemoryStats] = OrderedDict()  # memory_id → stats (LRU order)

    def record_recall(self, memory_id: str, relevance_score: float) -> None:
        """Record that a memory was recalled with a given relevance score."""
        now = time.monotonic()

        if memory_id not in self._stats:
            self._stats[memory_id] = _MemoryStats(created_at=now)

        stats = self._stats[memory_id]
        stats.recall_count += 1
        stats.last_recalled_at = now
        stats.total_relevance += relevance_score

        # Move to end (most recently used) — O(1) with OrderedDict
        self._stats.move_to_end(memory_id)

        # Evict LRU if over capacity
        while len(self._stats) > self.max_entries:
            self._stats.popitem(last=False)

    def record_creation(self, memory_id: str) -> None:
        """Record that a new memory was created."""
        now = time.monotonic()
        self._stats[memory_id] = _MemoryStats(created_at=now)

        # Move to end (most recently used) — O(1) with OrderedDict
        self._stats.move_to_end(memory_id)

        # Evict LRU if over capacity
        while len(self._stats) > self.max_entries:
            self._stats.popitem(last=False)

    def get_utility(self, memory_id: str) -> UtilityScore | None:
        """Compute current utility score for a memory. Returns None if not tracked."""
        stats = self._stats.get(memory_id)
        if stats is None:
            return None

        now = time.monotonic()
        avg_relevance = stats.total_relevance / max(stats.recall_count, 1)
        last_recalled_ago = now - stats.last_recalled_at if stats.last_recalled_at > 0 else now - stats.created_at
        created_ago = now - stats.created_at

        return compute_utility(
            recall_count=stats.recall_count,
            last_recalled_seconds_ago=last_recalled_ago,
            avg_relevance=avg_relevance,
            created_seconds_ago=created_ago,
            recency_half_life_days=self.recency_half_life_days,
        )

    def get_recall_count(self, memory_id: str) -> int:
        """Get the recall count for a memory."""
        stats = self._stats.get(memory_id)
        return stats.recall_count if stats else 0

    def clear(self) -> None:
        """Clear all tracked stats."""
        self._stats.clear()
