"""Signal quality policies — deduplication and noisy-bank detection.

All functions are sync (Rust migration candidates).
See docs/_design/policy-layer.md section 3.
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable

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

    def find_duplicate(
        self,
        bank_id: str,
        embedding: list[float],
        threshold_override: float | None = None,
        text: str | None = None,
    ) -> str | None:
        """Like :meth:`is_duplicate`, but returns the cached memory id the
        embedding duplicates (the first match, under the same threshold and
        negation guard), or ``None``."""
        entries = self._cache.get(bank_id, [])
        if entries:
            self._touch_bank(bank_id)
        for memory_id, cached_emb, cached_text in entries:
            pair = [(cosine_similarity(embedding, cached_emb), cached_text)]
            if self.matches(pair, threshold_override=threshold_override, text=text)[0]:
                return memory_id
        return None

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


class NoisyBankDetector:
    """Flags banks whose recent retains look like noise (policy-layer.md §3.3).

    Three signals, each from retains this process has seen for the bank:

    - ``retain_spike``: the last minute's retain count exceeds
      ``retain_spike_multiplier`` × the per-minute average before it (a runaway
      agent loop);
    - ``short_content``: the average length of recent retains is below
      ``min_avg_content_length`` characters (junk);
    - ``high_dedup_rate``: more than ``max_dedup_rate`` of recent retains were
      near-duplicates (redundant).

    Samples expire after ``HORIZON_SECONDS``, so a flag clears once the bank
    behaves — or, when the caller throttles or rejects flagged retains (which
    are then never recorded), once the window has passed. In-memory and
    per-process like ``DedupDetector``; thresholds are passed to ``check`` so
    each bank can use its own ``noisy_bank`` settings.
    """

    _MAX_BANKS = 1000
    #: Most recent retains the content-length and dedup-rate signals look at.
    SAMPLE_WINDOW = 50
    #: Below this many samples those two signals stay quiet: a new bank's first
    #: few short notes are not a pattern.
    MIN_SAMPLES = 20
    #: Samples older than this are forgotten.
    HORIZON_SECONDS = 3600.0
    #: A spike needs at least this many retains in the last minute...
    MIN_BURST = 10
    #: ...and at least this much earlier history in the window to compare with.
    MIN_BASELINE_SECONDS = 300.0
    _MAX_EVENTS_PER_BANK = 10_000

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # bank_id -> (timestamp, content_length, deduplicated), oldest first.
        self._events: dict[str, deque[tuple[float, int, bool]]] = {}
        # bank_id -> reasons it was flagged for at its last check.
        self._flagged: dict[str, tuple[str, ...]] = {}

    def record(self, bank_id: str, content_length: int, deduplicated: bool) -> None:
        """Record one processed retain (stored or deduplicated)."""
        events = self._events.pop(bank_id, None)
        if events is None:
            events = deque(maxlen=self._MAX_EVENTS_PER_BANK)
        self._events[bank_id] = events  # re-insert: most recently used last
        events.append((self._clock(), content_length, deduplicated))
        while len(self._events) > self._MAX_BANKS:
            evicted = next(iter(self._events))
            del self._events[evicted]
            self._flagged.pop(evicted, None)

    def check(
        self,
        bank_id: str,
        *,
        retain_spike_multiplier: float,
        min_avg_content_length: int,
        max_dedup_rate: float,
    ) -> tuple[tuple[str, ...], bool]:
        """Return ``(reasons, changed)``: why the bank is flagged now (empty
        when it is not), and whether that differs from its previous check — so
        callers can log transitions instead of every flagged retain."""
        reasons = self._reasons(bank_id, retain_spike_multiplier, min_avg_content_length, max_dedup_rate)
        changed = reasons != self._flagged.get(bank_id, ())
        if reasons:
            self._flagged[bank_id] = reasons
        else:
            self._flagged.pop(bank_id, None)
        return reasons, changed

    def _reasons(
        self,
        bank_id: str,
        spike_multiplier: float,
        min_avg_length: int,
        max_dedup_rate: float,
    ) -> tuple[str, ...]:
        events = self._events.get(bank_id)
        if not events:
            return ()
        now = self._clock()
        while events and events[0][0] < now - self.HORIZON_SECONDS:
            events.popleft()
        if not events:
            return ()
        reasons: list[str] = []

        minute_ago = now - 60.0
        last_minute = sum(1 for ts, _, _ in events if ts > minute_ago)
        earlier = len(events) - last_minute
        baseline_seconds = minute_ago - events[0][0]
        if last_minute >= self.MIN_BURST and earlier and baseline_seconds >= self.MIN_BASELINE_SECONDS:
            # Floor of one retain a minute: a quiet bank waking up to a handful
            # of writes is not a runaway loop.
            baseline_per_minute = max(earlier / (baseline_seconds / 60.0), 1.0)
            if last_minute > spike_multiplier * baseline_per_minute:
                reasons.append("retain_spike")

        recent = list(events)[-self.SAMPLE_WINDOW :]
        if len(recent) >= self.MIN_SAMPLES:
            if sum(length for _, length, _ in recent) / len(recent) < min_avg_length:
                reasons.append("short_content")
            if sum(1 for _, _, dup in recent if dup) / len(recent) > max_dedup_rate:
                reasons.append("high_dedup_rate")
        return tuple(reasons)
