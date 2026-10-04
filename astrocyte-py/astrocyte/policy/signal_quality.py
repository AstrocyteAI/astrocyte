"""Signal quality policies — deduplication detection.

All functions are sync (Rust migration candidates).
See docs/_design/policy-layer.md section 3.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

# Explicit negators. Deliberately a closed list: this guard targets the
# measurable failure (a statement and its negation embed as near-duplicates),
# not contradiction in general. Antonym reversals ("enable"/"disable") carry no
# negator and are NOT caught here — they also score lower (0.870 measured vs
# 0.920 for "allergic"/"not allergic"), so they sit further below threshold.
_NEGATORS = frozenset({"not", "no", "never", "neither", "nor", "without", "cannot", "none", "nobody", "nothing"})
_WORD = re.compile(r"\b\w+(?:'\w+)*\b")


def _tokens(text: str) -> list[str]:
    return _WORD.findall((text or "").casefold().replace("\u2019", "'"))


def _is_negator(token: str) -> bool:
    return token in _NEGATORS or token.endswith("n't")


def differs_by_negation(a: str, b: str) -> bool:
    """True when the token difference between ``a`` and ``b`` includes a negator.

    Embedding similarity cannot see the scope of a negation. Measured with
    bge-small: "The user is allergic to peanuts." vs "The user is not allergic
    to peanuts." scores 0.920 cosine, against 0.396 for an unrelated pair. A
    cosine-only dedup would treat the reversal as the duplicate and drop it.

    Why "differs by" rather than "either side contains": dedup operates on
    whole conversation chunks, and a long chunk almost always contains a
    negator somewhere. Requiring exact equality whenever one appears (the rule
    engrim uses for short curated records) would disable dedup for re-ingested
    conversations that differ only in a timestamp. Asking whether the
    *difference* contains a negator targets exactly the reversal case and
    leaves ordinary re-ingest dedup intact.

    Multiset difference, so "not ... not" vs "not" still counts.
    """
    ca, cb = Counter(_tokens(a)), Counter(_tokens(b))
    diff = (ca - cb) + (cb - ca)
    return any(_is_negator(t) for t in diff)


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
        # bank_id -> list of (memory_id, embedding, text). ``text`` is None for
        # entries added without it; those fall back to cosine-only.
        self._cache: dict[str, list[tuple[str, list[float], str | None]]] = {}
        #: How often the negation guard overrode a cosine match. Observable so
        #: the guard's effect can be measured rather than assumed.
        self.negation_overrides: int = 0

    def _touch_bank(self, bank_id: str) -> None:
        """Move bank to end of dict (most recently used) for LRU eviction."""
        if bank_id in self._cache:
            self._cache[bank_id] = self._cache.pop(bank_id)

    def is_duplicate(
        self,
        bank_id: str,
        embedding: list[float],
        threshold_override: float | None = None,
        text: str | None = None,
    ) -> tuple[bool, float]:
        """Check if embedding is a near-duplicate of cached content.

        ``threshold_override`` lets a per-call MIP DedupSpec.threshold take
        precedence over the instance-level default. When ``None``, the
        configured ``self.threshold`` is used.

        When ``text`` is given and the cached entry has text too, a cosine match
        is vetoed if the two differ by a negator (see ``differs_by_negation``).
        The asymmetry is deliberate: the default dedup action *drops* the new
        chunk, so a false duplicate silently loses a reversed fact, while a
        false non-duplicate only stores a redundant row. Bias toward keeping.

        Returns (is_dup, max_similarity).
        """
        entries = self._cache.get(bank_id, [])
        if entries:
            self._touch_bank(bank_id)
        scored = ((cosine_similarity(embedding, cached_emb), cached_text) for _, cached_emb, cached_text in entries)
        return self.matches(scored, threshold_override=threshold_override, text=text)

    def matches(
        self,
        candidates: Iterable[tuple[float, str | None]],
        threshold_override: float | None = None,
        text: str | None = None,
    ) -> tuple[bool, float]:
        """Apply the duplicate rule to ``(similarity, text)`` pairs scored elsewhere.

        Shared by the cache scan above and by the retain pipeline's check
        against the vector store's nearest neighbours, so both apply the same
        threshold and the same negation guard. Returns (is_dup, max_similarity).
        """
        threshold = threshold_override if threshold_override is not None else self.threshold
        max_sim = 0.0
        for sim, other_text in candidates:
            max_sim = max(max_sim, sim)
            if sim >= threshold:
                if text is not None and other_text is not None and differs_by_negation(text, other_text):
                    self.negation_overrides += 1
                    continue  # a reversal, not a duplicate — keep looking
                return True, sim
        return False, max_sim

    def add(self, bank_id: str, memory_id: str, embedding: list[float], text: str | None = None) -> None:
        """Add an embedding to the cache for future dedup checks.

        Pass ``text`` to enable the negation guard against this entry.
        """
        if bank_id not in self._cache:
            if len(self._cache) >= self._MAX_BANKS:
                # Evict least-recently-used bank (first key in insertion-ordered dict)
                lru_bank = next(iter(self._cache))
                del self._cache[lru_bank]
            self._cache[bank_id] = []
        self._touch_bank(bank_id)

        entries = self._cache[bank_id]
        entries.append((memory_id, embedding, text))

        # Evict oldest if over capacity
        if len(entries) > self.max_cache:
            self._cache[bank_id] = entries[-self.max_cache :]

    def clear_bank(self, bank_id: str) -> None:
        """Clear cache for a bank."""
        self._cache.pop(bank_id, None)

    def remove(self, bank_id: str, memory_id: str) -> bool:
        """Drop a single ``(memory_id, embedding)`` entry from the cache.

        Called from the forget pipeline so a re-retain of similar content
        after forget produces a fresh row instead of silently dedup'ing
        against the cached embedding of the now-deleted memory.

        Returns ``True`` if an entry was removed, ``False`` otherwise.
        Idempotent — multiple forgets of the same id are safe.
        """
        entries = self._cache.get(bank_id)
        if not entries:
            return False

        before = len(entries)
        self._cache[bank_id] = [e for e in entries if e[0] != memory_id]

        # Reclaim the bank slot entirely if it became empty.
        if not self._cache[bank_id]:
            del self._cache[bank_id]

        return len(self._cache.get(bank_id, [])) < before
