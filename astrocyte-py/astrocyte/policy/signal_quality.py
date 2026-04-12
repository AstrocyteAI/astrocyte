"""Signal quality policies — deduplication detection.

All functions are sync (Rust migration candidates).
See docs/_design/policy-layer.md section 3.
"""

from __future__ import annotations

import math


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Returns value in [-1.0, 1.0]. Returns 0.0 for zero vectors.
    Sync, pure computation — Rust migration candidate.
    """
    if len(a) != len(b):
        raise ValueError(f"Vector dimension mismatch: {len(a)} != {len(b)}")

    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot / (norm_a * norm_b)


class DedupDetector:
    """Detect near-duplicate content via embedding similarity.

    Stores recent embeddings per bank for comparison.
    Sync, self-contained — Rust migration candidate.
    """

    _MAX_BANKS = 1000

    def __init__(self, similarity_threshold: float = 0.95, max_cache_per_bank: int = 1000) -> None:
        self.threshold = similarity_threshold
        self.max_cache = max_cache_per_bank
        # bank_id -> list of (memory_id, embedding)
        self._cache: dict[str, list[tuple[str, list[float]]]] = {}

    def _touch_bank(self, bank_id: str) -> None:
        """Move bank to end of dict (most recently used) for LRU eviction."""
        if bank_id in self._cache:
            self._cache[bank_id] = self._cache.pop(bank_id)

    def is_duplicate(self, bank_id: str, embedding: list[float]) -> tuple[bool, float]:
        """Check if embedding is a near-duplicate of cached content.

        Returns (is_dup, max_similarity).
        """
        entries = self._cache.get(bank_id, [])
        if entries:
            self._touch_bank(bank_id)
        max_sim = 0.0

        for _, cached_emb in entries:
            sim = cosine_similarity(embedding, cached_emb)
            max_sim = max(max_sim, sim)
            if sim >= self.threshold:
                return True, sim

        return False, max_sim

    def add(self, bank_id: str, memory_id: str, embedding: list[float]) -> None:
        """Add an embedding to the cache for future dedup checks."""
        if bank_id not in self._cache:
            if len(self._cache) >= self._MAX_BANKS:
                # Evict least-recently-used bank (first key in insertion-ordered dict)
                lru_bank = next(iter(self._cache))
                del self._cache[lru_bank]
            self._cache[bank_id] = []
        self._touch_bank(bank_id)

        entries = self._cache[bank_id]
        entries.append((memory_id, embedding))

        # Evict oldest if over capacity
        if len(entries) > self.max_cache:
            self._cache[bank_id] = entries[-self.max_cache :]

    def clear_bank(self, bank_id: str) -> None:
        """Clear cache for a bank."""
        self._cache.pop(bank_id, None)
