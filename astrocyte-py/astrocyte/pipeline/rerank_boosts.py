"""Post-rerank multiplicative boosts (M14 Gap 3 closure).

After the cross-encoder rerank in ``section_rerank.py`` produces a
score for each candidate, we compose three additional signals
multiplicatively:

1. **Recency boost** — exponential decay on session age. More recent
   sessions score higher. ``half_life_days=180`` matches Hindsight's
   ~365-day linear curve at the midpoint; bounded to [0.5, 1.0] so a
   stale section is at most halved, never zeroed.

2. **Temporal-band intersection** — when the question carries a
   ``date_range`` (from ``question_annotator``), sections whose own
   time range (session_date or [occurred_start, occurred_end]) overlap
   the question's band get a multiplicative bonus. Sections with no
   date are neutral (multiplier 1.0).

3. **Proof-count boost** — log-normalised count of facts linked to a
   section. Sections backed by more atomic facts are stronger
   evidence. Bounded to [1.0, 1.5].

Composition is multiplicative (Hindsight pattern, see
``hindsight-api-slim/hindsight_api/engine/search/reranking.py``):

    final = CE_score × recency × temporal_band × proof_count

The multiplicative form avoids cancellation (an additive scheme can
let a strong negative signal wipe out a strong positive). With the
default alphas the worst case is ~-19% and the best ~+21%, so the
cross-encoder remains the primary ordering signal — the boosts
nudge ties and adjacent ranks.

Gated behind a config flag (``RerankBoostConfig.enabled``) so the
component can be ablated independently for bench gates. Default ON
once the gate clears.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrocyte.pipeline.section_recall import FusedHit
    from astrocyte.types import PageIndexSection


UTC = timezone.utc


# Default multiplicative alphas — match Hindsight's published values.
# Each signal contributes at most ±(alpha/2) relative adjustment.
_RECENCY_ALPHA: float = 0.2  # ±10%
_TEMPORAL_ALPHA: float = 0.2  # ±10%
_PROOF_COUNT_ALPHA: float = 0.1  # ±5%


@dataclass
class RerankBoostConfig:
    """Toggle + tuning for post-rerank boosts. Default values mirror
    Hindsight's tuned defaults (see ``reranking.py`` upstream).
    """

    enabled: bool = True
    recency_alpha: float = _RECENCY_ALPHA
    temporal_alpha: float = _TEMPORAL_ALPHA
    proof_count_alpha: float = _PROOF_COUNT_ALPHA
    #: Linear-decay window for recency. Sections older than this fall
    #: to the floor (0.1). 365 days matches Hindsight's default.
    recency_window_days: float = 365.0


def _section_date(section: PageIndexSection | None) -> datetime | None:
    """Pick the best timestamp for recency: prefer event date over
    session date when both are present, since the event date is when
    the *content* actually happened.
    """
    if section is None:
        return None
    return section.occurred_start or section.session_date


def _normalize_to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def recency_score(
    section_date: datetime | None,
    now: datetime,
    *,
    window_days: float = 365.0,
) -> float:
    """Linear decay over ``window_days`` → [0.1, 1.0]; neutral 0.5 if
    no date. Same shape Hindsight uses; floor of 0.1 keeps very-old
    items from being de-ranked to oblivion.
    """
    if section_date is None:
        return 0.5
    section_date = _normalize_to_utc(section_date)
    now = _normalize_to_utc(now)
    days_ago = (now - section_date).total_seconds() / 86400.0
    return max(0.1, min(1.0, 1.0 - (days_ago / window_days)))


def temporal_band_score(
    section: PageIndexSection | None,
    query_range: tuple[datetime, datetime] | None,
) -> float:
    """0.5 neutral when the question has no date band OR the section
    has no date. 1.0 when the section's date range overlaps the
    query's. 0.2 when both are dated and disjoint (penalise wrong-
    period sections — they were probably promoted by topical relevance
    despite missing the temporal target).
    """
    if query_range is None or section is None:
        return 0.5
    q_start, q_end = query_range
    q_start = _normalize_to_utc(q_start)
    q_end = _normalize_to_utc(q_end)

    # Prefer event range when populated, else collapse to session_date.
    s_start = section.occurred_start or section.session_date
    s_end = section.occurred_end or section.occurred_start or section.session_date
    if s_start is None or s_end is None:
        return 0.5
    s_start = _normalize_to_utc(s_start)
    s_end = _normalize_to_utc(s_end)

    # Overlap iff start <= other.end and end >= other.start.
    if s_start <= q_end and s_end >= q_start:
        return 1.0
    return 0.2


def proof_count_score(proof_count: int | None) -> float:
    """Log-normalised mapping ``proof_count`` → [0.5, 1.0]. A single-
    fact section is neutral (0.5); high-evidence sections approach the
    cap. Sections with no fact count attached are also neutral so the
    boost collapses to 1.0.
    """
    if proof_count is None or proof_count < 1:
        return 0.5
    # log curve centred at 0.5, clamped to 1.0 around proof_count=150.
    return min(1.0, max(0.0, 0.5 + (math.log(proof_count) / 10.0)))


def apply_boosts(
    hits: list[FusedHit],
    sections_by_key: dict[tuple[str, int], PageIndexSection],
    *,
    query_range: tuple[datetime, datetime] | None = None,
    proof_counts: dict[tuple[str, int], int] | None = None,
    now: datetime | None = None,
    config: RerankBoostConfig | None = None,
) -> list[FusedHit]:
    """Apply multiplicative post-rerank boosts and re-sort.

    Args:
      hits: Cross-encoder-reranked hits. ``rrf_score`` carries the CE
        score after ``section_rerank.rerank_fused_hits`` runs.
      sections_by_key: ``(document_id, line_num) → PageIndexSection``
        for date lookups.
      query_range: Inferred date band from question_annotator. ``None``
        collapses the temporal_band boost to 1.0.
      proof_counts: Optional ``(document_id, line_num) → int`` mapping
        of facts linked to each section. ``None`` collapses the
        proof_count boost to 1.0.
      now: Reference timestamp for recency. Defaults to ``datetime.now(UTC)``.
      config: Toggles + alphas. ``None`` uses defaults (enabled=True).

    Returns:
      A new sorted list. Input is not mutated. When ``config.enabled``
      is False, returns ``hits`` unchanged.
    """
    if config is None:
        config = RerankBoostConfig()
    if not config.enabled or not hits:
        return list(hits)

    if now is None:
        now = datetime.now(UTC)
    now = _normalize_to_utc(now)

    from astrocyte.pipeline.section_recall import FusedHit  # avoid circular import

    boosted: list[FusedHit] = []
    for h in hits:
        section = sections_by_key.get((h.document_id, h.line_num))
        section_dt = _section_date(section)

        recency = recency_score(
            section_dt,
            now,
            window_days=config.recency_window_days,
        )
        temporal = temporal_band_score(section, query_range)
        proof = proof_count_score(
            (proof_counts or {}).get((h.document_id, h.line_num)),
        )

        recency_boost = 1.0 + config.recency_alpha * (recency - 0.5)
        temporal_boost = 1.0 + config.temporal_alpha * (temporal - 0.5)
        proof_count_boost = 1.0 + config.proof_count_alpha * (proof - 0.5)

        new_score = h.rrf_score * recency_boost * temporal_boost * proof_count_boost

        boosted.append(
            FusedHit(
                document_id=h.document_id,
                line_num=h.line_num,
                rrf_score=new_score,
                per_strategy_rank=dict(h.per_strategy_rank),
            )
        )

    boosted.sort(key=lambda h: h.rrf_score, reverse=True)
    return boosted
