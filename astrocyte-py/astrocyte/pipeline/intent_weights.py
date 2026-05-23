"""Intent → RRF channel weights mapping (M34).

Maps :class:`~astrocyte.pipeline.query_intent.QueryIntent` to per-channel
weights for :func:`~astrocyte.pipeline.fusion.weighted_rrf_fusion`.

Why this exists
---------------

Pre-M34, ``fact_recall`` ran 4-5 retrieval channels (semantic, episodic,
temporal, link-expansion, BM25) and fused them with **equal-weight RRF**.
The v015i / v015j bench runs showed this single-pool architecture
shuffles ~3-4 questions across LME categories when temporal coverage
shifts — gains in temporal-reasoning come at the cost of
single-session-preference (and vice versa). Knob-tuning (capping
``top_k_temporal``) couldn't break the trade-off because the channels
compete on rank inside the same fused pool.

M34's intent layer fixes this by **biasing channel contribution per
query intent**. Temporal questions boost the temporal channel; preference
questions damp it. Equal-weight fallback (UNKNOWN intent) keeps the
pre-M34 behaviour for legacy callers and queries we can't classify.

Design choices
--------------

- **All weights in [0.0, 1.5]** — bounded range keeps RRF stable. Negative
  weights are rejected by ``weighted_rrf_fusion``; we use ``0.0`` to mute
  a channel rather than skip it conditionally in calling code.
- **Asymmetric biases** — the strongest boost (1.5) is reserved for the
  channel an intent depends on; the strongest dampening (0.2-0.3) for
  channels that introduce noise for that intent. Most channels stay at
  1.0 (neutral).
- **No 0.0 weights in the production table** — every channel gets at
  least 0.2. Hard mutes invite silent failures when the classifier
  misfires; soft dampening preserves graceful degradation.

References
----------

- Design doc: ``docs/_design/m34-query-intent-routing.md``
- Forensic basis: ``docs/_design/m31-lme-quality.md`` §8 (M31c
  anti-composition) + ``benchmark-results/.../astrocyte-v015{i,j}``
- Hindsight parallel: ``hindsight-api-slim/.../memory_engine.py:3009-3211``
  uses per-fact-type segmentation + conditional channel arity; M34 is
  the recall-bias analogue (combined with per-fact-type segmentation
  via M34-4).
"""

from __future__ import annotations

from dataclasses import dataclass

from astrocyte.pipeline.query_intent import QueryIntent


@dataclass(frozen=True)
class ChannelWeights:
    """RRF weights for each fact-recall channel.

    All weights must be ``>= 0.0`` (enforced by
    :func:`~astrocyte.pipeline.fusion.weighted_rrf_fusion`). A weight of
    ``0.0`` mutes the channel; a weight of ``1.0`` is neutral; values
    above ``1.0`` boost the channel's reciprocal-rank contribution.

    Channel names match the keyword arguments of ``fact_recall``:

    - ``semantic`` — cosine over fact-text embeddings
    - ``episodic`` — episodic-marker entity search (M18a-4)
    - ``temporal`` — date-range filter via search_facts_temporal
    - ``link_expansion`` — cross-session entity graph (M27)
    - ``bm25`` — full-text/BM25 over fact_text (M31c, re-wired in M34-5)
    """

    semantic: float = 1.0
    episodic: float = 1.0
    temporal: float = 1.0
    link_expansion: float = 1.0
    bm25: float = 1.0


#: Per-intent channel weight table. The single calibration knob of M34.
#:
#: Calibrated against the v015i/v015j failure modes:
#:
#: - SSP regressed -5 when temporal flooded → PREFERENCE-style intent
#:   damps temporal to 0.2.
#: - MS regressed -3 from cross-session dilution → RELATIONAL boosts
#:   link_expansion to 1.5.
#: - SSU held at 7/15 because BM25 was off → FACTUAL boosts bm25 to 1.5.
#: - TR held its +2 in both runs → TEMPORAL keeps temporal at 1.5.
#:
#: UNKNOWN (the safe fallback) gets the same weight profile as the
#: v015j "all equal but slightly damped temporal" setup — never worse
#: than current production behaviour for unclassifiable queries.
INTENT_CHANNEL_WEIGHTS: dict[QueryIntent, ChannelWeights] = {
    QueryIntent.TEMPORAL: ChannelWeights(
        semantic=1.0, episodic=0.7, temporal=1.5, link_expansion=0.5, bm25=1.0,
    ),
    QueryIntent.COMPARATIVE: ChannelWeights(
        semantic=1.0, episodic=1.0, temporal=0.3, link_expansion=1.0, bm25=1.0,
    ),
    QueryIntent.RELATIONAL: ChannelWeights(
        semantic=0.8, episodic=1.0, temporal=0.5, link_expansion=1.5, bm25=1.0,
    ),
    QueryIntent.FACTUAL: ChannelWeights(
        semantic=1.5, episodic=0.5, temporal=0.3, link_expansion=0.5, bm25=1.5,
    ),
    QueryIntent.PROCEDURAL: ChannelWeights(
        semantic=1.2, episodic=0.8, temporal=0.3, link_expansion=0.8, bm25=1.0,
    ),
    QueryIntent.EXPLORATORY: ChannelWeights(
        semantic=1.0, episodic=1.0, temporal=1.0, link_expansion=1.0, bm25=1.0,
    ),
    QueryIntent.UNKNOWN: ChannelWeights(
        semantic=1.0, episodic=1.0, temporal=0.5, link_expansion=1.0, bm25=1.0,
    ),
}


#: Neutral baseline. Identical to ``INTENT_CHANNEL_WEIGHTS[UNKNOWN]``
#: but exposed as a constant for callers that want to opt out of intent
#: routing without thinking about which fallback to pick.
NEUTRAL_WEIGHTS: ChannelWeights = INTENT_CHANNEL_WEIGHTS[QueryIntent.UNKNOWN]


def weights_for_intent(intent: QueryIntent | None) -> ChannelWeights:
    """Look up channel weights for an intent.

    Args:
        intent: Classified intent, or ``None`` to use the neutral
            baseline. ``None`` and :attr:`QueryIntent.UNKNOWN` resolve to
            the same baseline — callers that classify and find UNKNOWN
            should pass UNKNOWN explicitly so debug logs / metrics
            distinguish "classifier ran and was uncertain" from "caller
            didn't classify".

    Returns:
        Frozen :class:`ChannelWeights`. Never raises; unknown enum
        values fall back to :data:`NEUTRAL_WEIGHTS`.
    """
    if intent is None:
        return NEUTRAL_WEIGHTS
    return INTENT_CHANNEL_WEIGHTS.get(intent, NEUTRAL_WEIGHTS)


__all__ = [
    "ChannelWeights",
    "INTENT_CHANNEL_WEIGHTS",
    "NEUTRAL_WEIGHTS",
    "weights_for_intent",
]
