"""Recent memory buffer — ring buffer of recently stored chunks for Tier 1 fuzzy matching.

Provides sub-5ms fuzzy text search on recent memories without embedding cost.
Handles typos and morphological variations via character-level similarity.

Sync, self-contained — Rust migration candidate.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from string import punctuation

from astrocyte.types import MemoryHit

#: Default max recent items per bank.
DEFAULT_MAX_PER_BANK = 100

#: Minimum character-level similarity for a token to count as a fuzzy match.
_TOKEN_SIMILARITY_THRESHOLD = 0.75

# Query words too common to be meaningful for fuzzy matching.
_STOP_WORDS = frozenset(
    "a an the is are was were be been being have has had do does did "
    "will would shall should may might can could of in on at to for "
    "with by from and or but not no nor so yet both either neither "
    "this that these those it its he she they them his her their "
    "who what which when where how i me my we our you your "
    "about also just like very much more than then some any".split()
)


@dataclass(slots=True)
class _RecentEntry:
    memory_id: str
    text: str
    tokens: list[str]  # pre-tokenized for fast matching
    bank_id: str
    metadata: dict | None = None


def _tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, remove stop words."""
    return [
        w
        for raw in text.lower().split()
        if len(w := raw.strip(punctuation)) > 1 and w not in _STOP_WORDS
    ]


def _fuzzy_token_score(query_tokens: list[str], memory_tokens: list[str]) -> float:
    """Score a memory against query tokens using character-level fuzzy matching.

    For each query token, find the best fuzzy match among memory tokens.
    A match counts if SequenceMatcher ratio >= threshold (handles typos).
    Returns fraction of query tokens that matched (0.0–1.0).
    """
    if not query_tokens:
        return 0.0
    if not memory_tokens:
        return 0.0

    matched = 0
    # Build set for exact-match fast path
    memory_set = set(memory_tokens)

    for qt in query_tokens:
        # Fast path: exact match
        if qt in memory_set:
            matched += 1
            continue
        # Slow path: fuzzy match (character-level similarity)
        best = 0.0
        for mt in memory_tokens:
            # Quick length filter — very different lengths can't match well
            if abs(len(qt) - len(mt)) > max(len(qt), len(mt)) * 0.5:
                continue
            ratio = SequenceMatcher(None, qt, mt).ratio()
            if ratio > best:
                best = ratio
                if best >= _TOKEN_SIMILARITY_THRESHOLD:
                    break  # Good enough, stop searching
        if best >= _TOKEN_SIMILARITY_THRESHOLD:
            matched += 1

    return matched / len(query_tokens)


class RecentMemoryBuffer:
    """Ring buffer of recently stored text chunks per bank for Tier 1 fuzzy matching.

    Designed for sub-5ms search on the last N stored memories per bank.
    Uses character-level fuzzy matching (SequenceMatcher) to handle typos
    and morphological variations that BM25 would miss.

    Thread-safe: all mutations are protected by a lock.
    """

    _MAX_BANKS = 500

    def __init__(self, max_per_bank: int = DEFAULT_MAX_PER_BANK) -> None:
        self.max_per_bank = max_per_bank
        self._buffers: dict[str, deque[_RecentEntry]] = {}
        self._lock = threading.Lock()

    def add(self, bank_id: str, memory_id: str, text: str, metadata: dict | None = None) -> None:
        """Add a recently stored chunk to the buffer."""
        tokens = _tokenize(text)
        if not tokens:
            return  # Skip empty/stop-word-only text

        entry = _RecentEntry(
            memory_id=memory_id,
            text=text,
            tokens=tokens,
            bank_id=bank_id,
            metadata=metadata,
        )

        with self._lock:
            if bank_id not in self._buffers:
                # Evict LRU bank if at capacity
                if len(self._buffers) >= self._MAX_BANKS:
                    lru_bank = next(iter(self._buffers))
                    del self._buffers[lru_bank]
                self._buffers[bank_id] = deque(maxlen=self.max_per_bank)
            self._buffers[bank_id].append(entry)

    def search(
        self,
        query: str,
        bank_id: str,
        limit: int = 10,
        min_score: float = 0.3,
    ) -> list[MemoryHit]:
        """Fuzzy search recent memories for a bank.

        Returns scored MemoryHits sorted by relevance, up to ``limit``.
        Typically completes in <5ms for buffers of 100 entries.
        """
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        with self._lock:
            entries = list(self._buffers.get(bank_id, []))

        scored: list[tuple[float, _RecentEntry]] = []
        for entry in entries:
            score = _fuzzy_token_score(query_tokens, entry.tokens)
            if score >= min_score:
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)

        return [
            MemoryHit(
                text=entry.text,
                score=score,
                metadata=entry.metadata,
                memory_id=entry.memory_id,
                bank_id=bank_id,
            )
            for score, entry in scored[:limit]
        ]

    def clear_bank(self, bank_id: str) -> None:
        """Clear buffer for a bank."""
        with self._lock:
            self._buffers.pop(bank_id, None)

    def size(self, bank_id: str | None = None) -> int:
        """Number of buffered entries (total or per bank)."""
        with self._lock:
            if bank_id:
                return len(self._buffers.get(bank_id, []))
            return sum(len(b) for b in self._buffers.values())
