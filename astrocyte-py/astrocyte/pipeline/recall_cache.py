"""Recall cache — LRU cache keyed by query embedding similarity.

Avoids redundant retrieval for repeated or similar queries.
Invalidated on retain (bank contents changed).

Sync, self-contained — Rust migration candidate.
Inspired by ByteRover's Tier 0/1 progressive retrieval.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from astrocyte.policy.signal_quality import cosine_similarity
from astrocyte.types import RecallResult


@dataclass
class _CacheEntry:
    query_vector: list[float]
    result: RecallResult
    timestamp: float


class RecallCache:
    """LRU recall cache with similarity-based lookup.

    Entries are keyed by (bank_id, query_vector). A cache hit occurs when
    cosine similarity between the query vector and a cached vector exceeds
    the threshold. Entries expire after ttl_seconds.

    Invalidate a bank's cache on retain (contents changed).
    """

    def __init__(
        self,
        similarity_threshold: float = 0.95,
        max_entries: int = 256,
        ttl_seconds: float = 300.0,
    ) -> None:
        self.similarity_threshold = similarity_threshold
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, list[_CacheEntry]] = {}  # bank_id → entries

    def get(self, bank_id: str, query_vector: list[float]) -> RecallResult | None:
        """Look up a cached recall result by query similarity.

        Returns None on miss. Evicts expired entries during lookup.
        """
        entries = self._cache.get(bank_id)
        if not entries:
            return None

        now = time.monotonic()

        # Evict expired entries
        entries[:] = [e for e in entries if (now - e.timestamp) < self.ttl_seconds]

        # Search for similar query
        for entry in entries:
            sim = cosine_similarity(query_vector, entry.query_vector)
            if sim >= self.similarity_threshold:
                # Move to end (LRU)
                entries.remove(entry)
                entries.append(entry)
                return entry.result

        return None

    def put(self, bank_id: str, query_vector: list[float], result: RecallResult) -> None:
        """Store a recall result in the cache."""
        if bank_id not in self._cache:
            self._cache[bank_id] = []

        entries = self._cache[bank_id]

        # Evict LRU if this bank is at capacity
        while len(entries) >= self.max_entries:
            entries.pop(0)

        # Enforce global capacity across all banks
        total = sum(len(e) for e in self._cache.values())
        while total >= self.max_entries * 4:  # Global cap: 4x per-bank limit
            # Evict oldest entry across all banks
            oldest_bank = None
            oldest_time = float("inf")
            for bid, bank_entries in self._cache.items():
                if bank_entries and bank_entries[0].timestamp < oldest_time:
                    oldest_time = bank_entries[0].timestamp
                    oldest_bank = bid
            if oldest_bank is not None:
                self._cache[oldest_bank].pop(0)
                if not self._cache[oldest_bank]:
                    del self._cache[oldest_bank]
                total -= 1
            else:
                break

        entries.append(
            _CacheEntry(
                query_vector=query_vector,
                result=result,
                timestamp=time.monotonic(),
            )
        )

    def invalidate_bank(self, bank_id: str) -> None:
        """Clear all cached results for a bank (called on retain)."""
        self._cache.pop(bank_id, None)

    def invalidate_all(self) -> None:
        """Clear the entire cache."""
        self._cache.clear()

    def size(self, bank_id: str | None = None) -> int:
        """Number of cached entries (total or per bank)."""
        if bank_id:
            return len(self._cache.get(bank_id, []))
        return sum(len(entries) for entries in self._cache.values())
