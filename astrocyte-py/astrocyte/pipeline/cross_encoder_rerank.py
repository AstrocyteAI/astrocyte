"""Cross-encoder final-stage reranker (Hindsight parity).

The retrieval stack uses a bi-encoder (embedding cosine) to fetch a
broad candidate set, then this module's cross-encoder to rerank the
top-K with full query/document attention. Cross-encoders score every
(query, candidate) pair jointly — slower per-pair than bi-encoders, but
substantially more accurate. The combination is the standard IR pattern
Hindsight uses (see ``hindsight-docs/docs/developer/configuration.md``):
default model ``cross-encoder/ms-marco-MiniLM-L-6-v2``, with pluggable
local / FlashRank / jina-mlx backends.

Design:

- :class:`CrossEncoderProtocol` defines the minimal scoring surface.
  Production uses :class:`SentenceTransformersCrossEncoder` (pulls
  ``sentence-transformers`` and ``torch`` from the optional
  ``[rerank]`` extras). Tests can pass a fake.
- :func:`cross_encoder_rerank` reuses :class:`ScoredItem` from
  :mod:`astrocyte.pipeline.reranking` so it slots into the existing
  pipeline at the same boundary as ``cross_encoder_like_rerank``.
- A module-level cache keys models by ``(model_name, force_cpu)`` so
  repeated calls within a process amortize the load.

Failure mode: when ``sentence-transformers`` isn't installed and no
explicit model is supplied, callers fall back to the heuristic
``cross_encoder_like_rerank``. The pipeline orchestrator threads this
fallback automatically based on config.
"""

from __future__ import annotations

import logging
from threading import Lock
from typing import Protocol, runtime_checkable

from astrocyte.pipeline.reranking import ScoredItem

_logger = logging.getLogger("astrocyte.cross_encoder_rerank")

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CrossEncoderProtocol(Protocol):
    """Minimal scoring surface a cross-encoder backend must implement.

    Returns a list of relevance scores in the same order as ``candidates``.
    Higher = more relevant. Scale is backend-specific (sentence-transformers
    cross-encoders return raw logits; FlashRank returns calibrated [0, 1]).
    Reranking is order-preserving against the score vector, so absolute
    scale doesn't matter — only relative ranking.
    """

    def score(self, query: str, candidates: list[str]) -> list[float]: pass


# ---------------------------------------------------------------------------
# Sentence-transformers backend (default production implementation)
# ---------------------------------------------------------------------------


class SentenceTransformersCrossEncoder:
    """Default backend wrapping ``sentence_transformers.CrossEncoder``.

    Loads on first call; raises a clear error if the dependency isn't
    installed. The Hindsight default model is
    ``cross-encoder/ms-marco-MiniLM-L-6-v2`` — small (~80MB), CPU-fast,
    and trained on MS MARCO passage ranking which transfers well to
    open-domain QA reranking.
    """

    def __init__(
        self,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        *,
        force_cpu: bool = False,
        max_length: int = 512,
    ) -> None:
        self.model_name = model_name
        self.force_cpu = force_cpu
        self.max_length = max_length
        self._model: object | None = None  # lazily populated on first score()

    def _load(self) -> object:
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import CrossEncoder  # type: ignore
        except ImportError as exc:  # pragma: no cover — import-time failure
            raise ImportError(
                "Cross-encoder reranking requires the 'sentence-transformers' "
                "package. Install with: pip install 'astrocyte[rerank]' "
                "(or: pip install sentence-transformers torch)."
            ) from exc

        kwargs: dict[str, object] = {"max_length": self.max_length}
        if self.force_cpu:
            kwargs["device"] = "cpu"

        _logger.info("Loading cross-encoder model %r (force_cpu=%s)", self.model_name, self.force_cpu)
        self._model = CrossEncoder(self.model_name, **kwargs)
        return self._model

    def score(self, query: str, candidates: list[str]) -> list[float]:
        if not candidates:
            return []
        model = self._load()
        pairs = [(query, candidate) for candidate in candidates]
        # ``CrossEncoder.predict`` returns a numpy array; convert to plain
        # floats so callers don't need numpy in their typing.
        raw = model.predict(pairs)  # type: ignore[attr-defined]
        return [float(score) for score in raw]


# ---------------------------------------------------------------------------
# Module-level model cache
# ---------------------------------------------------------------------------

_model_cache: dict[tuple[str, bool], CrossEncoderProtocol] = {}
_cache_lock = Lock()


def get_default_cross_encoder(
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
    *,
    force_cpu: bool = False,
) -> CrossEncoderProtocol:
    """Return a cached :class:`SentenceTransformersCrossEncoder`.

    Threadsafe — concurrent first-load calls block on a lock so the
    model is only loaded once. Subsequent calls return the cached
    instance immediately.
    """
    key = (model_name, force_cpu)
    with _cache_lock:
        cached = _model_cache.get(key)
        if cached is None:
            cached = SentenceTransformersCrossEncoder(
                model_name, force_cpu=force_cpu,
            )
            _model_cache[key] = cached
        return cached


def reset_default_cross_encoder_cache() -> None:
    """Drop cached cross-encoder instances. Test-only."""
    with _cache_lock:
        _model_cache.clear()


# ---------------------------------------------------------------------------
# Reranking entry point
# ---------------------------------------------------------------------------


def cross_encoder_rerank(
    items: list[ScoredItem],
    query: str,
    *,
    model: CrossEncoderProtocol | None = None,
    top_k: int | None = None,
) -> list[ScoredItem]:
    """Rerank ``items`` by a cross-encoder's joint relevance score.

    Args:
        items: Candidate items to rerank — typically the top-K from a
            cheaper retrieval stage (bi-encoder or BM25).
        query: The user query / synthesis prompt fragment to score
            candidates against.
        model: Cross-encoder backend. Defaults to the cached
            :class:`SentenceTransformersCrossEncoder` with the Hindsight
            default model.
        top_k: When set, only the first ``top_k`` items are rescored;
            the remainder is appended after the reranked head with their
            original scores. Bounds inference cost on long candidate
            lists. Default ``None`` (rescore everything).

    Returns:
        Items sorted by descending cross-encoder score. Items beyond
        ``top_k`` retain their original score and follow the reranked
        head in their original relative order.
    """
    if not items or not query:
        return items

    if model is None:
        model = get_default_cross_encoder()

    head = items if top_k is None else items[:top_k]
    tail = [] if top_k is None else items[top_k:]

    scores = model.score(query, [item.text for item in head])
    if len(scores) != len(head):  # pragma: no cover — backend contract violation
        _logger.warning(
            "cross_encoder model returned %d scores for %d items; falling "
            "back to original order.",
            len(scores), len(head),
        )
        return items

    rescored = [
        ScoredItem(
            id=item.id,
            text=item.text,
            score=float(score),
            fact_type=item.fact_type,
            metadata=item.metadata,
            tags=item.tags,
            memory_layer=item.memory_layer,
            occurred_at=item.occurred_at,
            retained_at=item.retained_at,
        )
        for item, score in zip(head, scores, strict=True)
    ]
    rescored.sort(key=lambda x: x.score, reverse=True)
    return rescored + tail
